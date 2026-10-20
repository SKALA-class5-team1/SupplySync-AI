"""
approval_registration 도메인의 MCP 도구 호출 (business-mcp, rag-mcp).

- 서버 주소: 설정(BUSINESS_MCP_URL, RAG_MCP_URL). 전송 방식은 Streamable HTTP.
- 인증: 모든 요청에 X-Internal-Api-Key 헤더(설정 INTERNAL_API_KEY). 없거나 틀리면 401.
- 쓰기 도구(business_register_quote)는 thread_id 와 Idempotency-Key 헤더로 중복 반영을 막는다.
- 서버 오류(429·503)는 2회까지 다시 시도하고, 입력 오류(도구가 isError 로 답함)는 바로 실패한다.
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional, Union

from app.approval_registration.schemas import AtpResult, DocHit, RegisterResult
from app.core.config import settings

logger = logging.getLogger(__name__)

_RETRY_STATUS = {429, 503}
_MAX_RETRY = 2


class ToolError(Exception):
    """MCP 도구가 입력 오류 등으로 실패했다고 답했을 때. 다시 시도해도 같은 결과라 재시도하지 않는다."""


def _server_url(name: str) -> str:
    """도구 이름으로 어느 MCP 서버를 부를지 정한다."""
    return settings.RAG_MCP_URL if name.startswith("rag_") else settings.BUSINESS_MCP_URL


def _build_headers(
    thread_id: Optional[str], idempotency_key: Optional[str]
) -> dict[str, str]:
    headers = {"X-Internal-Api-Key": settings.INTERNAL_API_KEY}
    if thread_id:
        headers["X-Thread-Id"] = thread_id  # TODO: 헤더 이름은 백엔드와 확정
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _parse_result(result: Any) -> Any:
    """MCP 도구 응답에서 JSON 값을 꺼낸다."""
    if getattr(result, "isError", False):
        text = " ".join(getattr(c, "text", "") for c in result.content)
        raise ToolError(text or "MCP 도구 호출 실패")
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


def _is_retryable(exc: BaseException) -> bool:
    """HTTP 429·503 이면 True. anyio 가 예외를 묶어 올리는 경우도 안쪽까지 본다."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in _RETRY_STATUS:
        return True
    return any(_is_retryable(e) for e in getattr(exc, "exceptions", ()))


@asynccontextmanager
async def _open_transport(url: str, headers: dict[str, str]):
    """Streamable HTTP 연결을 연다. MCP SDK 버전에 따라 열기 방식이 달라 둘 다 지원한다."""
    try:  # 최신 SDK: 헤더는 HTTP 클라이언트에 담아 넘긴다
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

        async with create_mcp_http_client(headers=headers) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                yield streams[0], streams[1]
    except ImportError:  # 예전 SDK: headers 인자를 직접 받는다
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(url, headers=headers) as streams:
            yield streams[0], streams[1]


async def _call_once(
    name: str, arguments: dict[str, Any], headers: dict[str, str]
) -> Any:
    # MCP SDK 는 호출할 때만 불러온다 (설치 전에도 나머지 코드와 테스트가 돌아가도록)
    from mcp import ClientSession

    async with _open_transport(_server_url(name), headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return _parse_result(await session.call_tool(name, arguments))


async def _call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    thread_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Any:
    """MCP 도구를 호출하고 JSON 응답을 돌려준다."""
    headers = _build_headers(thread_id, idempotency_key)
    for attempt in range(_MAX_RETRY + 1):
        try:
            return await _call_once(name, arguments, headers)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - 재시도 대상인지 아래에서 가린다
            if attempt < _MAX_RETRY and _is_retryable(exc):
                logger.warning("MCP %s 재시도 %d/%d", name, attempt + 1, _MAX_RETRY)
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise


async def business_get_atp(
    model_id: str, options: list[str], qty: int, due_date: str
) -> AtpResult:
    """POST /internal/atp - 모델·옵션 기준 BOM 부품의 가용 재고와 부족분."""
    raw = await _call_tool(
        "business_get_atp",
        {"model_id": model_id, "options": options, "qty": qty, "due_date": due_date},
    )
    return AtpResult.model_validate(raw)


async def business_register_quote(
    quote_id: Union[int, str],
    approved_by: int,
    *,
    thread_id: str,
    idempotency_key: str,
) -> RegisterResult:
    """POST /internal/quotes/{id}/register - 사람 승인과 ATP 재확인을 통과한 뒤에만 호출한다."""
    raw = await _call_tool(
        "business_register_quote",
        {"quote_id": quote_id, "approved_by": approved_by},
        thread_id=thread_id,
        idempotency_key=idempotency_key,
    )
    return RegisterResult.model_validate(raw)


async def rag_search_docs(
    query: str, doc_type: Optional[str] = None, k: int = 4
) -> list[DocHit]:
    """VectorDB doc_chunks 조회 (bge-m3). SG4에서는 승인 안내용 규정 근거를 찾는다."""
    arguments: dict[str, Any] = {"query": query, "k": k}
    if doc_type:
        arguments["doc_type"] = doc_type
    raw = await _call_tool("rag_search_docs", arguments)
    return [DocHit.model_validate(item) for item in raw]
