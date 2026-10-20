"""
approval_registration 도메인의 MCP 도구 호출 (business-mcp, rag-mcp).

실제 연동 때는 _call_tool 하나만 MCP 클라이언트로 바꾸면 된다.
서버 주소는 설정(BUSINESS_MCP_URL, RAG_MCP_URL)을 따른다.
쓰기 도구(business_register_quote)는 thread_id 와 Idempotency-Key 로 중복 반영을 막는다.
"""
from typing import Any, Optional, Union

from app.approval_registration.schemas import AtpResult, DocHit, RegisterResult


async def _call_tool(
    name: str,
    arguments: dict[str, Any],
    *,
    thread_id: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Any:
    """MCP 도구를 호출하고 JSON 응답을 돌려준다.

    TODO: MCP 클라이언트(Streamable HTTP) 연결. 입력 오류는 ToolError, 429·503은 2회 재시도.
    """
    raise NotImplementedError("MCP 클라이언트 연결 후 구현")


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
