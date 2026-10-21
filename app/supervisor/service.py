"""메모리 체크포인트와 접수 기록은 단일 서버 프로세스의 수명 동안 유지한다."""

import asyncio
import logging
from dataclasses import dataclass
from functools import lru_cache

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from langgraph.types import Command

from app.approval_registration.graph import _parse_resume
from app.core.config import settings
from app.core.observability import run_config
from app.quote_drafting.harness import describe_error
from app.supervisor.events import ProgressEvents, send_event
from app.supervisor.graph import build_graph
from app.supervisor.schemas import (
    ApprovalResume, ClarificationResume, PendingInput, ResumeRunRequest,
    RunAcceptance, RunResult, StartRunRequest,
)

logger = logging.getLogger(__name__)


@dataclass
class AcceptedRun:
    request: StartRunRequest
    result: RunResult | None = None

    def receipt(self) -> RunAcceptance:
        return RunAcceptance(
            task_id=self.request.task_id, request_id=self.request.request_id,
            thread_id=self.request.thread_id,
        )


class WorkflowService:
    def __init__(self, graph=None):
        self.graph = graph if graph is not None else build_graph()
        self._locks: dict[str, asyncio.Lock] = {}
        self._requests: dict[str, AcceptedRun] = {}
        self._threads: dict[str, str] = {}
        self._tasks: dict[str, str] = {}
        self._background: set[asyncio.Task] = set()
        self._claimed_interrupts: set[tuple[str, str]] = set()
        self._closing = False

    def _lock(self, thread_id: str) -> asyncio.Lock:
        return self._locks.setdefault(thread_id, asyncio.Lock())

    def _schedule(self, thread_id: str, value, *, first: bool = False, interrupt_id: str | None = None) -> None:
        task = asyncio.create_task(
            self._execute(thread_id, value, first=first, interrupt_id=interrupt_id),
            name=f"workflow:{thread_id}",
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _execute(self, thread_id: str, value, *, first: bool, interrupt_id: str | None) -> None:
        # HTTP 접수는 이 잠금을 기다리지 않는다. 같은 thread의 그래프 실행만 직렬화한다.
        async with self._lock(thread_id):
            accepted = self._requests[self._threads[thread_id]]
            accepted.result = await self._run(thread_id, value, first=first, interrupt_id=interrupt_id)

    async def shutdown(self) -> None:
        """새 명령을 막고 이미 접수한 작업의 이벤트 전송까지 마친다."""
        self._closing = True
        if self._background:
            await asyncio.gather(*tuple(self._background))

    def _check_accepting(self) -> None:
        if self._closing:
            raise HTTPException(503, "서버 종료 중이어서 새 실행을 접수할 수 없습니다")

    @staticmethod
    def _config(thread_id: str) -> dict:
        return {**run_config(thread_id), "recursion_limit": 150}

    async def _snapshot(self, thread_id: str):
        return await self.graph.aget_state(self._config(thread_id), subgraphs=True)

    @staticmethod
    def _pending(snapshot) -> PendingInput | None:
        for task in snapshot.tasks:
            if task.interrupts:
                paused = task.interrupts[0]
                return PendingInput(id=paused.id, payload=jsonable_encoder(paused.value))
        return None

    async def get(self, thread_id: str) -> RunResult:
        snapshot = await self._snapshot(thread_id)
        if not snapshot.values:
            request_id = self._threads.get(thread_id)
            if request_id:
                accepted = self._requests[request_id]
                if accepted.result is not None:
                    return accepted.result.model_copy(deep=True)
                return RunResult(
                    **accepted.receipt().model_dump(), current_stage="SG1", next_step="rfq_review",
                    state=accepted.request.model_dump(mode="json", exclude_none=True),
                )
            raise HTTPException(404, "존재하지 않는 실행 ID입니다")
        state = jsonable_encoder(snapshot.values)
        pending = None if state.get("status") == "FAILED" else self._pending(snapshot)
        claimed = pending and (thread_id, pending.id) in self._claimed_interrupts
        status = "WAITING" if pending else state.get("status", "ANALYZING")
        stage = pending.payload["step"].split(".")[0] if pending else state.get("current_stage", "SG1")
        if claimed:
            pending, status = None, "RUNNING"
        return RunResult(
            task_id=state["task_id"], request_id=state["request_id"],
            thread_id=thread_id, status=status, current_stage=stage,
            next_step=pending.payload["step"] if pending else state.get("next_step", "hold"),
            waiting_for=pending, state={**state, "status": status, "current_stage": stage},
        )

    async def _notify_result(self, job: RunResult) -> None:
        # ainvoke 종료 후 읽은 체크포인트만 사용한다. 담당자가 즉시 재개해도 대기 지점이 존재한다.
        if job.waiting_for:
            payload = job.waiting_for.payload
            clarification = payload["step"] == "SG1.human_confirm"
            await send_event(job.task_id, {
                "type": "CLARIFICATION_REQUIRED" if clarification else "APPROVAL_REQUIRED",
                "step": "SPEC_MATCH" if clarification else "APPROVAL",
                "message": "RFQ 요구 확인이 필요합니다" if clarification else "견적 승인이 필요합니다",
                "data": {
                    **{key: value for key, value in payload.items() if key != "event"},
                    "interrupt_id": job.waiting_for.id, "thread_id": job.thread_id,
                    **({"quote_id": job.state["quote_id"]} if not clarification else {}),
                },
            })
        elif job.status == "ON_HOLD":
            await send_event(job.task_id, {
                "type": "RUN_ON_HOLD", "message": job.state.get("error") or "업무 검토가 필요합니다",
                "data": {"thread_id": job.thread_id, "final_plan": job.state.get("final_plan", {})},
            })
        elif job.status in {"DONE", "REJECTED"}:
            await send_event(job.task_id, {
                "type": "RUN_END",
                "data": {"outcome": "REGISTERED" if job.status == "DONE" else "REJECTED"},
            })
        else:
            raise RuntimeError(job.state.get("error") or f"예상하지 못한 실행 상태: {job.status}")

    async def _run(
        self, thread_id: str, value, *, first: bool = False, interrupt_id: str | None = None,
    ) -> RunResult:
        accepted = self._requests[self._threads[thread_id]]
        request = accepted.request
        progress = ProgressEvents(request.task_id)
        config = {**self._config(thread_id), "callbacks": [progress]}
        try:
            async with asyncio.timeout(settings.RUN_TIMEOUT_SEC):
                if interrupt_id:
                    snapshot = await self._snapshot(thread_id)
                    if snapshot.values.get("status") == "FAILED":
                        # 이전 실행의 대기 이벤트 전송이 실패했다면 이미 RUN_FAILED로 종료됐다.
                        return await self.get(thread_id)
                    pending = self._pending(snapshot)
                    if not pending or pending.id != interrupt_id:
                        raise RuntimeError("접수한 재개 요청의 체크포인트가 변경되었습니다")
                if first:
                    await progress.start("RFQ_ANALYSIS")
                await self.graph.ainvoke(value, config)
                job = await self.get(thread_id)
                if job.status == "FAILED":
                    raise RuntimeError(job.state.get("error") or "그래프 실행 실패")
                await progress.finish()
                await self._notify_result(job)
                return job
        except asyncio.CancelledError:
            return await self._fail(thread_id, "백그라운드 실행이 취소되었습니다")
        except Exception as exc:
            # 쓰기의 응답이 불명확해도 그래프를 자동 재실행하지 않는다.
            timeout = isinstance(exc, TimeoutError)
            error = (
                f"실행 제한 시간({settings.RUN_TIMEOUT_SEC:g}초)을 초과했습니다"
                if timeout else describe_error(exc)
            )
            return await self._fail(thread_id, error)

    async def _fail(self, thread_id: str, error: str) -> RunResult:
        accepted = self._requests[self._threads[thread_id]]
        request = accepted.request
        logger.error("워크플로 실패 thread_id=%s %s", thread_id, error)
        failed = {
            **request.model_dump(mode="json", exclude_none=True),
            "status": "FAILED", "next_step": "hold", "error": error,
        }
        accepted.result = RunResult(
            task_id=request.task_id, request_id=request.request_id, thread_id=thread_id,
            status="FAILED", current_stage="SG1", next_step="hold", state=failed,
        )
        try:
            await self.graph.aupdate_state(self._config(thread_id), failed)
            accepted.result = await self.get(thread_id)
        except Exception:
            logger.exception("실패 체크포인트 기록 오류 thread_id=%s", thread_id)
        try:
            await send_event(request.task_id, {"type": "RUN_FAILED", "message": error, "data": {"error": error}})
        except Exception:
            logger.exception("RUN_FAILED 전송 실패 thread_id=%s", thread_id)
        return accepted.result

    async def start(self, request: StartRunRequest) -> RunAcceptance:
        existing = self._requests.get(request.request_id)
        if existing:
            if existing.request != request:
                raise HTTPException(409, "같은 request_id에 다른 실행 본문을 사용할 수 없습니다")
            return existing.receipt()
        self._check_accepting()
        if request.thread_id in self._threads or request.task_id in self._tasks:
            raise HTTPException(409, "이미 접수한 task_id 또는 thread_id입니다. 기존 request_id를 사용하세요")
        # 첫 await 전에 접수하므로 동시에 도착한 동일 요청도 한 번만 시작된다.
        accepted = AcceptedRun(request.model_copy(deep=True))
        self._requests[request.request_id] = accepted
        self._threads[request.thread_id] = request.request_id
        self._tasks[request.task_id] = request.thread_id
        self._schedule(request.thread_id, request.model_dump(mode="json", exclude_none=True), first=True)
        return accepted.receipt()

    @staticmethod
    def _resume_value(job: RunResult, request: ResumeRunRequest) -> dict:
        pending = job.waiting_for
        if not pending:
            raise HTTPException(409, "담당자 입력을 기다리는 실행이 아닙니다")
        step = pending.payload["step"]
        if isinstance(request, ClarificationResume):
            if (request.thread_id, request.task_id) != (job.thread_id, job.task_id):
                raise HTTPException(409, "재개 요청의 task_id 또는 thread_id가 현재 실행과 다릅니다")
            if step != "SG1.human_confirm":
                raise HTTPException(409, "현재 요구 확인 단계가 아닙니다")
            values = {item.field: item.value for item in request.decision.assumptions}
            fields = {item["field"] for item in pending.payload["items"]}
            if len(values) != len(request.decision.assumptions) or set(values) - fields:
                raise HTTPException(422, "assumptions는 확인 요청 필드를 중복 없이 포함해야 합니다")
            return {"step": step, "approved": True, "user_id": request.userId, "values": values}
        if isinstance(request, ApprovalResume):
            if step != request.step:
                raise HTTPException(409, "현재 견적 승인 단계가 아닙니다")
            value = request.model_dump(exclude_none=True)
            if request.quoteId is not None:
                value["quote_id"] = value.pop("quoteId")
            try:
                _parse_resume(value, pending.payload["card"]["quote_id"])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return value
        raise HTTPException(422, "지원하지 않는 재개 본문입니다")

    async def resume(self, thread_id: str, request: ResumeRunRequest) -> RunAcceptance:
        self._check_accepting()
        # 동시에 온 두 승인이 다음 revision의 interrupt까지 소비하지 않도록 대기 ID를 먼저 고정한다.
        initial = await self.get(thread_id)
        value = self._resume_value(initial, request)
        key = (thread_id, initial.waiting_for.id)
        # snapshot 조회 이후 claim과 예약 사이에는 await가 없다.
        if key in self._claimed_interrupts:
            raise HTTPException(409, "이미 접수한 대기 요청입니다")
        self._claimed_interrupts.add(key)
        self._schedule(thread_id, Command(resume={key[1]: value}), interrupt_id=key[1])
        return self._requests[self._threads[thread_id]].receipt()


@lru_cache(maxsize=1)
def get_service() -> WorkflowService:
    return WorkflowService()


async def shutdown_service() -> None:
    if get_service.cache_info().currsize:
        await get_service().shutdown()
        get_service.cache_clear()
