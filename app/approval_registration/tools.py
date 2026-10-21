"""
approval_registration 도메인의 MCP 도구 호출 (business-mcp, rag-mcp).

- 서버 주소: 설정(BUSINESS_MCP_URL, RAG_MCP_URL). 전송 방식은 Streamable HTTP.
- 인증: 모든 요청에 X-Internal-Api-Key 헤더(설정 INTERNAL_API_KEY). 없거나 틀리면 401.
- 도구 이름은 서버가 정한 이름 그대로 부른다 (check_parts_supply 등). business_ 접두어는 Agent 쪽
  MCP 클라이언트가 붙이는 이름이라 여기서는 쓰지 않는다.
- 중복 등록 방지(Idempotency-Key)는 business-mcp 서버가 백엔드로 보낼 때 붙인다. 이쪽은 보내지 않는다.
- 서버 오류(429·503)와 시간 초과는 2회까지 다시 시도하고(전체 TASK_TIMEOUT_SEC 안에서), 입력 오류(도구가 isError 로 답함)는 바로 실패한다.
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional, Union

from app.approval_registration.schemas import DocHit, RegisterResult, SupplyResult
from app.core.config import settings
from app.core.guardrails import mask_pii, truncate_any, truncate_text

logger = logging.getLogger(__name__)

_RETRY_STATUS = {429, 503}
_MAX_RETRY = 2


class ToolError(Exception):
    """MCP 도구가 입력 오류 등으로 실패했다고 답했을 때. 다시 시도해도 같은 결과라 재시도하지 않는다."""


def _server_url(server: str) -> str:
    return settings.RAG_MCP_URL if server == "rag" else settings.BUSINESS_MCP_URL


def _build_headers() -> dict[str, str]:
    return {"X-Internal-Api-Key": settings.INTERNAL_API_KEY}


def _parse_result(result: Any) -> Any:
    """MCP 도구 응답에서 JSON 값을 꺼낸다."""
    if getattr(result, "isError", False):
        text = " ".join(getattr(c, "text", "") for c in result.content)
        raise ToolError(
            truncate_text(mask_pii(text), settings.MCP_RESULT_MAX_CHARS) or "MCP 도구 호출 실패"
        )
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


def _find_tool_error(exc: BaseException) -> Optional[ToolError]:
    """anyio 가 예외를 ExceptionGroup 으로 감싸 올리므로, 안쪽의 ToolError 를 찾아 꺼낸다."""
    if isinstance(exc, ToolError):
        return exc
    for inner in getattr(exc, "exceptions", ()):
        found = _find_tool_error(inner)
        if found:
            return found
    return None


def _is_retryable(exc: BaseException) -> bool:
    """HTTP 429·503 이거나 시간 초과면 True. anyio 가 예외를 묶어 올리는 경우도 안쪽까지 본다."""
    if isinstance(exc, asyncio.TimeoutError):
        return True
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
    server: str, name: str, arguments: dict[str, Any], headers: dict[str, str]
) -> Any:
    # MCP SDK 는 호출할 때만 불러온다 (설치 전에도 나머지 코드와 테스트가 돌아가도록)
    from mcp import ClientSession

    async with _open_transport(_server_url(server), headers) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return _parse_result(await session.call_tool(name, arguments))


async def _call_tool(server: str, name: str, arguments: dict[str, Any]) -> Any:
    """MCP 도구를 호출하고 JSON 응답을 돌려준다. server 는 "business" 또는 "rag"."""
    headers = _build_headers()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.TASK_TIMEOUT_SEC  # 재시도를 합쳐 이 시간을 넘기지 않는다
    for attempt in range(_MAX_RETRY + 1):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError(f"MCP {name} 작업 제한 시간 초과")
        try:
            # 호출 1회는 MCP_CALL_TIMEOUT_SEC, 전체는 남은 시간 안에 끊는다 (시간 초과도 재시도 대상)
            return await asyncio.wait_for(
                _call_once(server, name, arguments, headers),
                min(settings.MCP_CALL_TIMEOUT_SEC, remaining),
            )
        except Exception as exc:  # noqa: BLE001 - 재시도 대상인지 아래에서 가린다
            tool_error = _find_tool_error(exc)
            if tool_error is not None:
                raise tool_error from None  # 도구가 오류로 답한 것 - 다시 시도해도 같다
            if attempt < _MAX_RETRY and _is_retryable(exc):
                logger.warning("MCP %s 재시도 %d/%d", name, attempt + 1, _MAX_RETRY)
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            raise


async def business_check_parts_supply(
    due_date: str, parts: list[dict[str, Any]], base_date: Optional[str] = None
) -> SupplyResult:
    """부품별 재고 충분 여부. 승인 후 재확인에서 SG2 의 최종 계획 수량으로 호출한다."""
    arguments: dict[str, Any] = {"due_date": due_date, "parts": parts}
    if base_date:
        arguments["base_date"] = base_date
    raw = await _call_tool("business", "check_parts_supply", arguments)
    return SupplyResult.model_validate(raw)


async def business_register_quote(quote_id: Union[int, str], thread_id: str) -> RegisterResult:
    """승인된 견적을 등록한다. 되돌리기 어려워 사람 승인과 재고 재확인을 통과한 뒤에만 호출한다."""
    raw = await _call_tool(
        "business", "register_quote", {"quote_id": quote_id, "thread_id": thread_id}
    )
    return RegisterResult.model_validate(raw)


async def rag_search_docs(
    query: str, doc_type: Optional[str] = None, k: int = 4
) -> list[DocHit]:
    """VectorDB doc_chunks 조회. SG4에서는 승인 안내용 규정 근거를 찾는다."""
    arguments: dict[str, Any] = {"query": query, "k": k}
    if doc_type:
        arguments["doc_type"] = doc_type
    raw = await _call_tool("rag", "rag_search_docs", arguments)
    # 규정 본문이 길 수 있어 상태로 넘기기 전에 길이를 자른다
    return [
        DocHit.model_validate(truncate_any(item, settings.MCP_RESULT_MAX_CHARS)) for item in raw
    ]
