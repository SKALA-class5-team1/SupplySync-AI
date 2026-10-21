"""실제 MCP SDK가 초기화·도구 요청에 인증 헤더를 싣는지 HTTP 경계에서 검증한다."""

import asyncio
import json

import httpx
import pytest

from app.approval_registration import tools as sg4
from app.core import mcp_client as business
from app.core.config import settings
from app.core.mcp_auth import get_mcp_headers
from app.quote_drafting import tools as sg3
from app.rfq_review import mcp_tools as sg1
from app.supply_analysis.mcp_tools import McpGateway


@pytest.mark.parametrize("mcp_key", ["", "test-mcp-key"])
def test_mcp_headers_support_shared_or_separate_key(monkeypatch, mcp_key):
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setattr(settings, "MCP_API_KEY", mcp_key)
    assert get_mcp_headers() == {
        "X-API-Key": mcp_key or "test-internal-key",
        "X-Internal-Api-Key": "test-internal-key",
    }


@pytest.fixture
def mcp_http(monkeypatch):
    # 전역 클라이언트가 생성 시 공통 인증 설정을 적용했는지 먼저 확인한다.
    # 실제 키는 assertion 출력이나 HTTP 대역으로 넘기지 않는다.
    connections = [
        business.mcp_client.connections[business.BUSINESS],
        sg3._rag_client.connections[sg3.RAG],
    ]
    for connection in connections:
        valid = connection["headers"] == get_mcp_headers()
        assert valid, "전역 MCP 클라이언트에 공통 인증 헤더가 적용되지 않았습니다"
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", "test-internal-key")
    monkeypatch.setattr(settings, "MCP_API_KEY", "test-mcp-key")
    for connection in connections:
        monkeypatch.setitem(connection, "headers", get_mcp_headers())

    methods = []
    original_client = httpx.AsyncClient

    def respond(request):
        authenticated = (
            request.headers.get("X-API-Key") == "test-mcp-key"
            and request.headers.get("X-Internal-Api-Key") == "test-internal-key"
        )
        if not authenticated:
            return httpx.Response(401)
        if request.method != "POST":
            return httpx.Response(405)
        payload = json.loads(request.content)
        method = payload["method"]
        methods.append(method)
        if "id" not in payload:
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": payload["params"]["protocolVersion"],
                "capabilities": {"tools": {}}, "serverInfo": {"name": "test", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": []}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": '{"ok":true}'}], "isError": False}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(respond),
    ))
    return methods


@pytest.mark.parametrize("stage,server", [
    ("common", "business"),
    ("SG1", "business"), ("SG1", "rag"),
    ("SG2", "business"), ("SG2", "rag"),
    ("SG3", "business"), ("SG3", "rag"),
    ("SG4", "business"), ("SG4", "rag"),
])
def test_mcp_requests_include_both_auth_headers(mcp_http, stage, server):
    async def run():
        if stage == "common":
            return await business.call_business_tool("get_bom", {})
        if stage == "SG1":
            return await sg1._call(server, "get_models" if server == "business" else "rag_search_docs", {})
        if stage == "SG2":
            return await McpGateway()._call_once("business_get_bom" if server == "business" else "rag_search_docs", {})
        if stage == "SG3":
            return await sg3._call_once(server, "get_options" if server == "business" else "rag_search_docs", {})
        return await sg4._call_once(server, "check_parts_supply" if server == "business" else "rag_search_docs",
                                    {}, sg4._build_headers())

    assert asyncio.run(run()) == {"ok": True}
    assert "initialize" in mcp_http and "tools/call" in mcp_http
