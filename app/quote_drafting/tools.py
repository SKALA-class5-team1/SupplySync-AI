"""
SG3 MCP 도구 호출 (business-mcp, rag-mcp)과 LLM에 넘기는 LangChain 도구

- business-mcp는 공통 클라이언트(app/core/mcp_client.py)의 세션을 쓴다. 도구 이름은 서버 이름 그대로(get_options 등).
- rag-mcp는 아직 공통 클라이언트에 없어 여기서 연결한다. 공통에 추가되면 그쪽을 쓴다.
- 읽기 도구는 429·503·시간 초과면 2회까지 다시 시도한다. 도구가 isError로 답하면(입력 오류 등) 바로 ToolError.
- 쓰기 도구(save_quote)는 다시 시도하지 않는다. 응답만 못 받고 실제로는 저장됐을 수 있어서다.
- 결과는 개인정보를 가리고 길이를 잘라 돌려준다.
"""
import asyncio
import json
import logging
from typing import Any, Optional

from langchain_core.tools import tool
from langchain_mcp_adapters.client import MultiServerMCPClient
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.guardrails import mask_any, mask_pii, truncate_any, truncate_text
from app.core.mcp_client import BUSINESS, mcp_client
from app.quote_drafting.harness import (
    MAX_RETRY,
    MCP_CALL_TIMEOUT_SEC,
    RAG_CHUNK_MAX_CHARS,
    RAG_TOP_K,
    RESULT_MAX_CHARS,
    RETRY_BACKOFF_SEC,
    is_retryable,
)
from app.quote_drafting.schemas import DocHit, OptionsResult, PartsCostResult, SaveQuoteResult

logger = logging.getLogger(__name__)

RAG = "rag"

_rag_client = MultiServerMCPClient(
    {
        RAG: {
            "transport": "streamable_http",
            "url": settings.RAG_MCP_URL,
            "headers": {"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
        },
    }
)


class ToolError(Exception):
    """MCP 도구가 오류로 답했을 때(isError). 다시 시도해도 같은 결과라 재시도하지 않는다."""


def _parse_result(result: Any) -> Any:
    """MCP 도구 응답에서 JSON 값을 꺼낸다."""
    if getattr(result, "isError", False):
        text = " ".join(getattr(c, "text", "") for c in result.content)
        raise ToolError(truncate_text(mask_pii(text), RESULT_MAX_CHARS) or "MCP 도구 호출 실패")
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        # 목록 응답은 {"result": [...]} 로 감싸져 오는 경우가 있다
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    for content in result.content:
        text = getattr(content, "text", None)
        if text:
            return json.loads(text)
    raise ToolError("MCP 도구 응답이 비어 있습니다")


async def _call_once(server: str, name: str, arguments: dict[str, Any]) -> Any:
    client = mcp_client if server == BUSINESS else _rag_client
    async with client.session(server) as session:
        result = await session.call_tool(name, arguments)
    return _parse_result(result)


async def _call_tool(
    server: str,
    name: str,
    arguments: dict[str, Any],
    *,
    retry: bool = True,
    deadline: Optional[float] = None,
) -> Any:
    """MCP 도구를 호출한다. 호출 1회는 MCP_CALL_TIMEOUT_SEC, 전체는 deadline(loop.time 기준)까지."""
    loop = asyncio.get_running_loop()
    attempts = MAX_RETRY + 1 if retry else 1
    for attempt in range(attempts):
        timeout = MCP_CALL_TIMEOUT_SEC
        if deadline is not None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError(f"MCP {name}: SG3 작업 제한 시간 초과")
            timeout = min(timeout, remaining)
        try:
            return await asyncio.wait_for(_call_once(server, name, arguments), timeout)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - 재시도 대상인지 아래에서 가린다
            if attempt < attempts - 1 and is_retryable(exc):
                logger.warning("SG3 MCP %s 재시도 %d/%d", name, attempt + 1, MAX_RETRY)
                await asyncio.sleep(RETRY_BACKOFF_SEC * (attempt + 1))
                continue
            raise


def _clean(raw: Any, max_chars: int = RESULT_MAX_CHARS) -> Any:
    return truncate_any(mask_any(raw), max_chars)


async def business_get_options(model_id: str, deadline: Optional[float] = None) -> OptionsResult:
    """모델 기본가와 옵션가."""
    raw = await _call_tool(BUSINESS, "get_options", {"model_id": model_id}, deadline=deadline)
    return OptionsResult.model_validate(_clean(raw))


async def business_get_parts_cost(
    model_id: str, options: list[str], deadline: Optional[float] = None
) -> PartsCostResult:
    """옵션을 반영한 부품 원가."""
    arguments = {"model_id": model_id, "options": options}
    raw = await _call_tool(BUSINESS, "get_parts_cost", arguments, deadline=deadline)
    return PartsCostResult.model_validate(_clean(raw))


async def business_save_quote(payload: dict[str, Any]) -> SaveQuoteResult:
    """견적 초안(DRAFT) 저장. 쓰기라서 다시 시도하지 않는다 (사람 승인 후에만 호출)."""
    raw = await _call_tool(BUSINESS, "save_quote", payload, retry=False)
    return SaveQuoteResult.model_validate(_clean(raw))


async def rag_search_docs(
    query: str, doc_type: str = "policy", k: int = RAG_TOP_K, deadline: Optional[float] = None
) -> list[DocHit]:
    """규정 근거 검색. 필요한 필드만, 조각마다 RAG_CHUNK_MAX_CHARS로 잘라 돌려준다.

    TODO: rag-mcp 레포가 비어 있어 도구 이름·입출력은 명세 예시 기준. 올라오면 확인.
    """
    raw = await _call_tool(RAG, "search_docs", {"query": query, "doc_type": doc_type, "k": k}, deadline=deadline)
    return [DocHit.model_validate(_clean(item, RAG_CHUNK_MAX_CHARS)) for item in raw[:k]]


# --- LLM에 bind_tools로 넘기는 도구. 실행은 그래프의 tools 노드가 한다 (호출 횟수·시간 한도·오류 처리) ---


class ConditionText(BaseModel):
    index: int = Field(description="입력 conditions의 index")
    text: str = Field(description="조건부 사항 문구 한 문장")


@tool
async def search_policy_docs(query: str) -> str:
    """사내 규정(영업 정책) 문서에서 조건 문구의 근거를 검색한다. query는 조항 번호(예: "제12조")나 짧은 주제어."""
    return json.dumps([h.model_dump() for h in await rag_search_docs(query)], ensure_ascii=False)


@tool
async def submit_conditions(conditions: list[ConditionText]) -> str:
    """모든 조건부 사항 문구를 한 번에 제출한다. 검증 결과가 도구 응답으로 돌아온다."""
    return "제출됨"


LLM_TOOLS = [search_policy_docs, submit_conditions]
