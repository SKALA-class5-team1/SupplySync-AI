import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.types import CallToolResult, TextContent

from app.supply_analysis.mcp_tools import McpGateway, ToolError, parse_result


def test_structured_only_rag_response():
    rows = [{"chunk_id": "alt-003", "content": "원문"}]
    result = CallToolResult(content=[], structuredContent={"result": rows})
    assert parse_result(result) == rows


def test_text_business_response_and_structured_precedence():
    result = CallToolResult(content=[TextContent(type="text", text=json.dumps({"parts": []}))])
    assert parse_result(result) == {"parts": []}
    result.structuredContent = {"parts": ["authoritative"]}
    assert parse_result(result) == {"parts": ["authoritative"]}


def test_error_even_when_structured_data_present():
    result = CallToolResult(isError=True, content=[TextContent(type="text", text="없는 부품")], structuredContent={"parts": []})
    with pytest.raises(ToolError, match="없는 부품"):
        parse_result(result)


@pytest.mark.parametrize("result", [CallToolResult(content=[]), CallToolResult(content=[TextContent(type="text", text="not json")])])
def test_malformed_result_is_rejected(result):
    with pytest.raises(ToolError):
        parse_result(result)


@pytest.mark.parametrize("error", [
    ToolError("백엔드 오류 (429): 너무 많은 요청"),
    ToolError("백엔드 오류 (503): 일시 장애"),
    ExceptionGroup("task", [ToolError("백엔드 오류 (503): 일시 장애")]),
    httpx.ConnectError("connection refused"),
])
def test_transient_errors_retry_twice(error):
    gateway = McpGateway(retry_delay=0)
    gateway._call_once = AsyncMock(side_effect=[error, error, {"ok": True}])
    assert asyncio.run(gateway.call("business_get_bom", model_id="A")) == {"ok": True}
    assert gateway._call_once.await_count == 3


def test_http_503_retry_and_nulls_omitted_but_empty_plan_preserved():
    error = httpx.HTTPStatusError("503", request=httpx.Request("POST", "http://localhost/mcp"), response=httpx.Response(503))
    gateway = McpGateway(retry_delay=0)
    gateway._call_once = AsyncMock(side_effect=[error, {}])
    asyncio.run(gateway.call("business_save_review", final_plan={}, recommended_alt=None))
    assert gateway._call_once.await_args.args == ("business_save_review", {"final_plan": {}})


def test_input_error_not_retried():
    gateway = McpGateway(retry_delay=0)
    gateway._call_once = AsyncMock(side_effect=ToolError("존재하지 않는 모델입니다"))
    with pytest.raises(ToolError, match="존재하지 않는 모델"):
        asyncio.run(gateway.call("business_get_bom"))
    assert gateway._call_once.await_count == 1


def test_retry_exhaustion_preserves_nested_cause():
    gateway = McpGateway(retry_delay=0)
    gateway._call_once = AsyncMock(side_effect=ExceptionGroup("task", [ToolError("백엔드 오류 (503): 과부하")]))
    with pytest.raises(ToolError, match="과부하"):
        asyncio.run(gateway.call("business_get_bom"))
    assert gateway._call_once.await_count == 3


def test_timeout_is_bounded():
    async def slow(*_):
        await asyncio.sleep(1)

    gateway = McpGateway(timeout=0.001, retry_delay=0)
    gateway._call_once = AsyncMock(side_effect=slow)
    with pytest.raises(ToolError, match="TimeoutError"):
        asyncio.run(gateway.call("business_get_bom"))
    assert gateway._call_once.await_count == 3
