"""BE 작업 이벤트. 사람 대기 이벤트는 WorkflowService가 체크포인트 저장 후 전송한다."""

import asyncio
from urllib.parse import quote

import httpx
from langchain_core.callbacks import AsyncCallbackHandler

from app.core.http_client import get_internal_api_client


class EventDeliveryError(RuntimeError):
    pass


async def post_once(task_id: str, event: dict) -> None:
    async with get_internal_api_client() as client:
        response = await client.post(
            f"/internal/tasks/{quote(task_id, safe='')}/events", json=event, timeout=3.0,
        )
    if response.status_code != 409:
        response.raise_for_status()


async def send_event(task_id: str, event: dict) -> None:
    for attempt in range(3):
        try:
            # 읽기·쓰기별 timeout 외에 전체 전송 시간도 제한한다.
            async with asyncio.timeout(3.0):
                await post_once(task_id, event)
            return
        except (httpx.HTTPError, TimeoutError) as exc:
            code = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
            if code == 409:
                return
            retryable = code is None or code in {408, 429} or code >= 500
            if not retryable or attempt == 2:
                detail = f"HTTP {code}" if code else type(exc).__name__
                raise EventDeliveryError(f"{event['type']} 이벤트 전송 실패: {detail}") from exc
            await asyncio.sleep(0.2 * (attempt + 1))


class ProgressEvents(AsyncCallbackHandler):
    raise_error = True

    _steps = {
        "parse_rfq": "RFQ_ANALYSIS",
        "validate_rules": "SPEC_MATCH",
        "human_confirm": "SPEC_MATCH",
        "match_spec": "SPEC_MATCH",
        "get_parts": "SUPPLY_CHECK",
        "build_alternatives": "ALTERNATIVE_ANALYSIS",
        "prepare_quote": "QUOTE",
        "save_quote": "FINAL_REVIEW",
        "prepare_approval_card": "APPROVAL",
        "request_approval": "APPROVAL",
        "recheck_supply": "REVALIDATION",
        "register_quote": "REGISTRATION",
    }
    _messages = {
        "RFQ_ANALYSIS": "RFQ 분석을 시작합니다",
        "SPEC_MATCH": "요구 사양을 검토합니다",
        "SUPPLY_CHECK": "부품 공급을 확인합니다",
        "ALTERNATIVE_ANALYSIS": "부족 부품의 대안을 분석합니다",
        "QUOTE": "견적을 작성합니다",
        "FINAL_REVIEW": "견적을 저장합니다",
        "APPROVAL": "견적 승인을 확인합니다",
        "REVALIDATION": "승인 후 재고를 다시 확인합니다",
        "REGISTRATION": "견적을 등록합니다",
    }

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._active: str | None = None

    async def start(self, step: str) -> None:
        if self._active == step:
            return
        await self.finish()
        await send_event(self.task_id, {"type": "STEP_START", "step": step, "message": self._messages[step]})
        self._active = step

    async def finish(self) -> None:
        if self._active:
            await send_event(self.task_id, {"type": "STEP_END", "step": self._active})
            self._active = None

    async def on_chain_start(self, serialized, inputs, *, run_id, name=None, **kwargs):
        if name in self._steps:
            await self.start(self._steps[name])

    async def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        if self._active:
            await send_event(self.task_id, {
                "type": "TOOL_START", "step": self._active,
                "data": {"tool": (serialized or {}).get("name"), "tool_run_id": str(run_id)},
            })

    async def on_tool_end(self, output, *, run_id, **kwargs):
        if self._active:
            await send_event(self.task_id, {
                "type": "TOOL_END", "step": self._active, "data": {"tool_run_id": str(run_id)},
            })
