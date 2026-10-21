"""모델 생성과 호출 복구 (Harness: Recovery)

429·503·시간 초과면 같은 모델로 2회 재시도하고, 계속 실패하면 대체 모델로 한 번 더 시도한다.
그 밖의 오류(입력 오류 등)는 다시 시도해도 같아 바로 올린다. 노드는 올라온 오류를 FAILED로 끝낸다.
"""
import asyncio
import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage
from langchain_core.runnables import Runnable

from app.core.config import settings
from app.quote_drafting.harness import (
    MAX_RETRY,
    MODEL_CALL_TIMEOUT_SEC,
    RETRY_BACKOFF_SEC,
    describe_error,
    is_retryable,
)
from app.quote_drafting.tools import LLM_TOOLS

logger = logging.getLogger(__name__)


class TaskTimeoutError(Exception):
    """SG3 작업 전체 제한 시간을 넘었다. 재시도 대상이 아니다."""


def create_chat_model(name: str) -> BaseChatModel:
    from langchain_openai import ChatOpenAI

    # 재시도는 이 모듈이 맡으므로 SDK 자체 재시도는 끈다
    return ChatOpenAI(model=name, api_key=settings.OPENAI_API_KEY, temperature=0, max_retries=0)


def bind_llm_tools(model: BaseChatModel) -> Runnable:
    return model.bind_tools(LLM_TOOLS)


async def _call(model: Runnable, messages: list[AnyMessage], deadline: float) -> AIMessage:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise TaskTimeoutError("SG3 작업 제한 시간 초과")
    return await asyncio.wait_for(model.ainvoke(messages), min(MODEL_CALL_TIMEOUT_SEC, remaining))


async def invoke_with_recovery(
    primary: Runnable, fallback: Runnable, messages: list[AnyMessage], deadline: float, trace_id: str
) -> AIMessage:
    for attempt in range(MAX_RETRY + 1):
        try:
            return await _call(primary, messages, deadline)
        except TaskTimeoutError:
            raise
        except Exception as exc:  # noqa: BLE001 - 재시도 대상인지 아래에서 가린다
            if not is_retryable(exc):
                raise
            logger.warning(
                "SG3 모델 호출 실패 %d/%d trace_id=%s %s", attempt + 1, MAX_RETRY + 1, trace_id, describe_error(exc)
            )
            if attempt < MAX_RETRY:
                await asyncio.sleep(RETRY_BACKOFF_SEC * (attempt + 1))
    logger.warning("SG3 대체 모델로 전환 trace_id=%s", trace_id)
    return await _call(fallback, messages, deadline)
