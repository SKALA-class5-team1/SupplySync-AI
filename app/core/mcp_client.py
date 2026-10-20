"""MCP 서버 공통 클라이언트. 각 SG 노드는 이 모듈로 business-mcp Tool을 쓴다.

- LLM에게 Tool을 맡길 때: get_business_tools() → 이름에 business_ 접두어 (예: business_get_bom)
- 코드에서 직접 호출할 때: call_business_tool("get_bom", {...}) → 결과 JSON을 dict/list로 반환

business-mcp는 MCP 표준대로 Tool 오류를 isError 결과(원인 메시지)로 돌려준다.
LangChain Tool로 부르면 오류도 성공처럼 텍스트로 돌아오므로, 코드에서 결과 값을 쓸 때는
isError를 확인해 McpToolError를 던지는 call_business_tool()을 쓴다.
"""

import json
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from app.core.config import settings

BUSINESS = "business"

# 서버 이름이 Tool 이름 접두어가 된다 (get_bom → business_get_bom).
mcp_client = MultiServerMCPClient(
    {
        BUSINESS: {
            "transport": "streamable_http",
            "url": settings.BUSINESS_MCP_URL,
            "headers": {"X-Internal-Api-Key": settings.BUSINESS_MCP_API_KEY},
        },
    },
    tool_name_prefix=True,
)


class McpToolError(Exception):
    """MCP Tool 실행 오류 (result.isError). 메시지는 서버가 보낸 원인 그대로다."""


def _require_api_key() -> None:
    # 키가 비어 있으면 서버가 원인을 알기 어려운 401로 끊으므로, 호출 전에 바로 알려준다
    if not settings.BUSINESS_MCP_API_KEY:
        raise RuntimeError(".env의 BUSINESS_MCP_API_KEY가 비어 있습니다. business-mcp의 INTERNAL_API_KEY와 같은 값을 넣으세요.")


async def get_business_tools() -> dict[str, BaseTool]:
    """LLM에 bind_tools로 넘길 business-mcp Tool. 키는 business_ 접두어가 붙은 이름."""
    _require_api_key()
    tools = await mcp_client.get_tools(server_name=BUSINESS)
    return {tool.name: tool for tool in tools}


async def call_business_tool(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """business-mcp Tool을 직접 호출해 결과 JSON을 돌려준다.

    name은 서버 쪽 이름(접두어 없음, 예: "get_parts_cost")이다.
    Tool 오류(isError)면 McpToolError를 던진다.
    """
    _require_api_key()
    async with mcp_client.session(BUSINESS) as session:
        result = await session.call_tool(name, arguments or {})

    text = "".join(block.text for block in result.content if block.type == "text")
    if result.isError:
        raise McpToolError(f"{name}: {text}")
    return json.loads(text)
