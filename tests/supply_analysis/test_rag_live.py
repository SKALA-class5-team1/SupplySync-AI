"""LIVE_RAG=true일 때 실제 RAG + 가짜 Business로 분석한다. 실제 DB 저장은 없다."""

import asyncio
import os

import pytest

from app.supply_analysis import deps
from app.supply_analysis.mcp_tools import McpGateway
from app.supply_analysis.service import analyze_supply

pytestmark = pytest.mark.skipif(os.getenv("LIVE_RAG") != "true", reason="LIVE_RAG=true일 때만 실행")


def test_real_rag_criteria_drive_demo_plan(gateway, request_data, monkeypatch, substitute_plan):
    real = McpGateway()

    class LiveRagGateway:
        async def call(self, name, **arguments):
            if name == "rag_search_docs":
                return await real.call(name, **arguments)
            return await gateway.call(name, **arguments)

    monkeypatch.setattr(deps, "gateway", lambda: LiveRagGateway())
    result = asyncio.run(analyze_supply(request_data))
    assert result.error is None
    assert result.recommended_alt == "SUBSTITUTE_PART"
    assert result.final_plan == substitute_plan
    assert result.selected_alternative.extra_cost_krw == 150000
    refs = {ref.chunk_id for ref in result.selected_alternative.parts[0].evidence}
    assert {"alt-001", "alt-002", "alt-003"} <= refs
