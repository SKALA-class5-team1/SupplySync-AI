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
        "match_spec": ("SPEC_MATCH", "요구 사양을 검토합니다"),
        "get_parts": ("SUPPLY_CHECK", "부품 공급을 확인합니다"),
        "build_alternatives": ("ALTERNATIVE_ANALYSIS", "부족 부품의 대안을 분석합니다"),
        "prepare_quote": ("QUOTE", "견적을 작성합니다"),
        "save_quote": ("FINAL_REVIEW", "견적을 저장합니다"),
        "recheck_supply": ("REVALIDATION", "승인 후 재고를 다시 확인합니다"),
        "register_quote": ("REGISTRATION", "견적을 등록합니다"),
    }

    def __init__(self, task_id: str):
        self.task_id = task_id

    async def on_chain_start(self, serialized, inputs, *, run_id, name=None, **kwargs):
        if name in self._steps:
            step, message = self._steps[name]
            await send_event(self.task_id, {"type": "STEP_START", "step": step, "message": message})
