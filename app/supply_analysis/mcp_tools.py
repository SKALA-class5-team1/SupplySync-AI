"""SG2의 임시 gateway. 기존 Business 클라이언트와 공통 설정을 재사용한다.

RAG는 아직 공통 클라이언트에 없으므로 SDK 세션만 이곳에서 연다.
공통 파일/클라이언트의 서버 목록을 변경하지 않는다. 노드는 deps.gateway로만 호출한다.
"""

import asyncio
import json
import logging
import re
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ToolError(RuntimeError):
    """MCP 오류 또는 잘못된 응답."""


def parse_result(result: Any) -> Any:
    text = " ".join(block.text for block in result.content if block.type == "text")
    if result.isError:
        raise ToolError(text or "MCP 도구 호출 실패")
    data = result.structuredContent
    if data is None:
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise ToolError("MCP 응답에 유효한 JSON이 없습니다") from exc
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def retryable(exc: Exception) -> bool:
    nested = getattr(exc, "exceptions", ())
    if nested:
        return all(isinstance(item, Exception) and retryable(item) for item in nested)
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 503}
    if isinstance(exc, (httpx.TransportError, TimeoutError)):
        return True
    return isinstance(exc, ToolError) and bool(re.search(r"백엔드 오류\s*\((429|503)\)", str(exc)))


class McpGateway:
    """첫 호출 + 2회 재시도. 쓰기는 thread_id upsert인 save_review만 허용한다."""

    def __init__(self, timeout: float = 60, retry_delay: float = 0.5):
        self.timeout = timeout
        self.retry_delay = retry_delay

    async def _call_once(self, name: str, arguments: dict[str, Any]) -> Any:
        from langchain_mcp_adapters.sessions import create_session

        from app.core.config import settings
        from app.core.mcp_client import BUSINESS, mcp_client

        if settings.INTERNAL_API_KEY in ("", "changeme-internal-key"):
            raise ToolError(".env의 INTERNAL_API_KEY를 MCP 서버와 같은 값으로 설정하세요")
        business_tools = {
            "business_get_bom": "get_bom",
            "business_check_parts_supply": "check_parts_supply",
            "business_get_substitutes": "get_substitutes",
            "business_save_review": "save_review",
        }
        if name in business_tools:
            async with mcp_client.session(BUSINESS) as session:
                return parse_result(await session.call_tool(business_tools[name], arguments))
        if name == "rag_search_docs":
            connection = {
                "transport": "streamable_http",
                "url": settings.RAG_MCP_URL,
                "headers": {"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            }
            async with create_session(connection) as session:
                await session.initialize()
                return parse_result(await session.call_tool("rag_search_docs", arguments))
        raise ToolError(f"SG2에서 허용하지 않는 도구: {name}")

    async def call(self, name: str, **arguments: Any) -> Any:
        arguments = {key: value for key, value in arguments.items() if value is not None}
        for attempt in range(3):
            try:
                async with asyncio.timeout(self.timeout):
                    return await self._call_once(name, arguments)
            except Exception as exc:
                if attempt < 2 and retryable(exc):
                    logger.warning("MCP retry tool=%s attempt=%d", name, attempt + 1)
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    continue
                # anyio ExceptionGroup에서도 서버 원인 메시지를 보존한다.
                def messages(error):
                    nested = getattr(error, "exceptions", ())
                    return "; ".join(messages(item) for item in nested) if nested else str(error)

                raise ToolError(f"{name}: {messages(exc) or type(exc).__name__}") from exc
