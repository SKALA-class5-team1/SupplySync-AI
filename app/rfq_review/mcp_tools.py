"""
SG1이 쓰는 MCP 도구 호출과 응답 변환.

노드는 이 파일의 함수만 호출하고 MCP 세부 사항(서버 이름, 헤더, 응답 형태)은 모릅니다.
백엔드 응답 필드명이 바뀌면 이 파일의 응답 모델만 고치면 됩니다.

- business-mcp: get_rfq_content, get_models, get_options, save_review
- rag-mcp: rag_search_docs
- 두 서버 모두 X-Internal-Api-Key 헤더(.env의 INTERNAL_API_KEY)로 인증합니다.
- 연결은 함수 호출 시점에 엽니다. 서버 시작이나 import 시점에는 연결하지 않습니다.
"""
import asyncio
import json
import logging
from typing import Any, Optional

import httpx
from langchain_mcp_adapters.client import MultiServerMCPClient
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict

from app.core.config import settings

logger = logging.getLogger(__name__)

BUSINESS = "business"
RAG = "rag"

MAX_RETRIES = 2
RETRY_STATUS = {429, 503}
RETRY_WAIT_SECONDS = 1.0


class McpToolError(RuntimeError):
    """MCP 도구가 오류 결과(isError)를 돌려준 경우."""


# ---------------------------------------------------------------------------
# 응답 모델
# ---------------------------------------------------------------------------
class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RfqContent(_Response):
    rfq_number: str
    customer: Optional[str] = None
    item: Optional[str] = None
    file_name: Optional[str] = None
    content_type: Optional[str] = None
    received_at: Optional[str] = None
    content: str


class ModelSpec(_Response):
    model_id: str
    model_name: Optional[str] = None
    flow_lpm: float
    head_kpa: float
    voltage_min: float
    voltage_max: float
    temp_min: float
    temp_max: float
    ip_rating: str
    noise_db: float
    comm: str
    size_l_mm: float
    size_w_mm: float
    size_h_mm: float
    weight_kg: float
    base_price_krw: int


class ModelOption(_Response):
    option_code: str
    option_name: Optional[str] = None
    changes_field: str
    new_value: str
    add_weight_kg: float = 0
    add_noise_db: float = 0
    add_price_krw: int
    replaces_part_id: Optional[str] = None
    uses_part_id: Optional[str] = None


class ModelOptions(_Response):
    model_id: str
    base_price_krw: Optional[int] = None
    options: list[ModelOption] = []


class DocChunk(_Response):
    chunk_id: str
    section: str
    content: str
    score: Optional[float] = None


class SaveReviewResult(_Response):
    review_id: int
    thread_id: str
    status: str


# ---------------------------------------------------------------------------
# 공통 호출
# ---------------------------------------------------------------------------
def _client() -> MultiServerMCPClient:
    headers = {"X-Internal-Api-Key": settings.INTERNAL_API_KEY}
    return MultiServerMCPClient(
        {
            BUSINESS: {"transport": "streamable_http", "url": settings.BUSINESS_MCP_URL, "headers": headers},
            RAG: {"transport": "streamable_http", "url": settings.RAG_MCP_URL, "headers": headers},
        }
    )


def _is_retryable(exc: BaseException) -> bool:
    """
    429·503이면 재시도 대상.
    - MCP 서버 자체의 HTTP 응답: 예외 체인(ExceptionGroup 포함) 안의 httpx 오류
    - 백엔드 오류: 도구 결과 메시지에 상태 코드 포함 (예: "백엔드 오류 (503): ...")
    """
    stack, seen = [exc], set()
    while stack:
        cur = stack.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, httpx.HTTPStatusError) and cur.response.status_code in RETRY_STATUS:
            return True
        if isinstance(cur, McpToolError) and any(f"({code})" in str(cur) for code in RETRY_STATUS):
            return True
        if isinstance(cur, BaseExceptionGroup):
            stack.extend(cur.exceptions)
        stack.extend([cur.__cause__, cur.__context__])
    return False


def _payload(tool: str, result: CallToolResult) -> Any:
    """도구 결과에서 JSON 값을 꺼냅니다. structuredContent를 우선 사용합니다."""
    texts = [c.text for c in result.content if isinstance(c, TextContent)]
    if result.isError:
        raise McpToolError(f"MCP 도구 '{tool}' 오류: {' '.join(texts)[:300]}")

    if result.structuredContent is not None:
        data = result.structuredContent
        # FastMCP는 list 반환값을 {"result": [...]}로 감쌉니다.
        if isinstance(data, dict) and set(data) == {"result"}:
            return data["result"]
        return data

    parsed = [json.loads(t) for t in texts if t.strip()]
    if not parsed:
        return None
    return parsed[0] if len(parsed) == 1 else parsed


async def _call(server: str, tool: str, args: dict) -> Any:
    for attempt in range(MAX_RETRIES + 1):
        try:
            async with _client().session(server) as session:
                result = await session.call_tool(tool, args)
            return _payload(tool, result)
        except Exception as exc:
            if attempt < MAX_RETRIES and _is_retryable(exc):
                logger.warning("MCP %s/%s 재시도 %d/%d", server, tool, attempt + 1, MAX_RETRIES)
                await asyncio.sleep(RETRY_WAIT_SECONDS * (attempt + 1))
                continue
            raise


def _as_list(data: Any, key: str) -> list:
    """[...] 또는 {key: [...]} 형태를 모두 받습니다."""
    if data is None:
        return []
    if isinstance(data, dict):
        return data.get(key, [])
    return data


# ---------------------------------------------------------------------------
# 도구 함수 (노드는 이 함수들만 사용)
# ---------------------------------------------------------------------------
async def get_rfq_content(rfq_number: str) -> RfqContent:
    data = await _call(BUSINESS, "get_rfq_content", {"rfq_number": rfq_number})
    return RfqContent.model_validate(data)


async def get_models() -> list[ModelSpec]:
    data = await _call(BUSINESS, "get_models", {})
    return [ModelSpec.model_validate(m) for m in _as_list(data, "models")]


async def get_options(model_id: str) -> ModelOptions:
    data = await _call(BUSINESS, "get_options", {"model_id": model_id})
    return ModelOptions.model_validate(data)


async def save_review(payload: dict) -> SaveReviewResult:
    data = await _call(BUSINESS, "save_review", payload)
    return SaveReviewResult.model_validate(data)


async def search_docs(query: str, doc_type: str, k: int = 2) -> list[DocChunk]:
    data = await _call(RAG, "rag_search_docs", {"query": query, "doc_type": doc_type, "k": k})
    return [DocChunk.model_validate(c) for c in _as_list(data, "results")]
