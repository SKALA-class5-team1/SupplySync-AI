"""동일 프로세스에서 시작·조회·재개 요청이 하나의 체크포인터를 사용한다."""

import asyncio
import logging
from functools import lru_cache

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from langgraph.types import Command

from app.approval_registration.graph import _parse_resume
from app.core.observability import run_config
from app.quote_drafting.harness import describe_error
from app.quote_drafting.nodes import _parse_save_decision
from app.supervisor.graph import build_graph
from app.supervisor.schemas import JobResult, PendingInput, ResumeJobRequest, StartJobRequest

logger = logging.getLogger(__name__)


class WorkflowService:
    def __init__(self, graph=None):
        self.graph = graph if graph is not None else build_graph()
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, thread_id: str) -> asyncio.Lock:
        return self._locks.setdefault(thread_id, asyncio.Lock())

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

    async def get(self, thread_id: str) -> JobResult:
        snapshot = await self._snapshot(thread_id)
        if not snapshot.values:
            raise HTTPException(404, "존재하지 않는 실행 ID입니다")
        state = jsonable_encoder(snapshot.values)
        pending = None if state.get("status") == "FAILED" else self._pending(snapshot)
        status = "WAITING" if pending else state.get("status", "ANALYZING")
        stage = pending.payload["step"].split(".")[0] if pending else state.get("current_stage", "SG1")
        return JobResult(
            thread_id=thread_id, status=status, current_stage=stage,
            next_step=pending.payload["step"] if pending else state.get("next_step", "hold"),
            waiting_for=pending, state={**state, "status": status, "current_stage": stage},
        )

    async def _run(self, thread_id: str, value) -> JobResult:
        config = self._config(thread_id)
        try:
            await self.graph.ainvoke(value, config)
        except Exception as exc:  # interrupt는 예외로 잡지 않는다. 외부 실패 후 쓰기를 자동 재실행하지 않는다.
            error = describe_error(exc)
            logger.error("워크플로 중단 thread_id=%s %s", thread_id, error)
            await self.graph.aupdate_state(config, {"status": "FAILED", "next_step": "hold", "error": error})
        return await self.get(thread_id)

    async def start(self, request: StartJobRequest) -> JobResult:
        async with self._lock(request.thread_id):
            existing = await self._snapshot(request.thread_id)
            if existing.values:
                raise HTTPException(409, "이미 시작한 실행 ID입니다. 상태 조회 또는 resume을 사용하세요")
            return await self._run(request.thread_id, request.model_dump(mode="json", exclude_none=True))

    async def resume(self, thread_id: str, request: ResumeJobRequest) -> JobResult:
        async with self._lock(thread_id):
            job = await self.get(thread_id)
            pending = job.waiting_for
            if not pending or pending.id != request.interrupt_id:
                raise HTTPException(409, "현재 대기 요청이 아닙니다. 최신 waiting_for.id로 재개하세요")
            value = request.response
            step = pending.payload["step"]
            if value.get("step") != step:
                raise HTTPException(409, "현재 대기 중인 step과 재개 요청의 step이 다릅니다")
            try:
                if step == "SG3.request_save_approval":
                    _parse_save_decision(value)
                elif step == "SG4.request_approval":
                    _parse_resume(value, pending.payload["card"]["quote_id"])
                elif step == "SG1.human_confirm":
                    if type(value.get("approved")) is not bool or type(value.get("user_id")) is not int:
                        raise ValueError("SG1 확인에는 approved(bool), user_id(int)가 필요합니다")
                    values = value.get("values", {})
                    fields = {item["field"] for item in pending.payload["items"]}
                    if not isinstance(values, dict) or set(values) - fields:
                        raise ValueError("values는 확인 요청에 포함된 필드만 수정할 수 있습니다")
                else:
                    raise ValueError("지원하지 않는 승인 단계입니다")
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return await self._run(thread_id, Command(resume={pending.id: value}))


@lru_cache(maxsize=1)
def get_service() -> WorkflowService:
    return WorkflowService()
