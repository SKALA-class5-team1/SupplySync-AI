"""business-mcp 실제 연결 확인. business-mcp 서버가 떠 있어야 하므로 기본 pytest에서는 건너뛴다.

실행: LIVE_MCP=true .venv/bin/python -m pytest tests/core/test_business_mcp_live.py -v
기대값은 v2.2 시드 기준 (SupplySync-Business-MCP의 CLAUDE.md 테스트 기대값).
"""

import asyncio
import os

import pytest

from app.core.mcp_client import McpToolError, call_business_tool, get_business_tools

pytestmark = pytest.mark.skipif(os.getenv("LIVE_MCP") != "true", reason="LIVE_MCP=true일 때만 실행")

B_OPTIONS = ["OPT-HT", "OPT-IP9K"]


def test_lists_business_tools_with_prefix():
    tools = asyncio.run(get_business_tools())

    assert set(tools) == {
        "business_get_models", "business_get_options", "business_get_bom",
        "business_check_parts_supply", "business_get_parts_cost",
        "business_save_review", "business_save_quote", "business_register_quote",
        "business_get_rfq_content", "business_get_substitutes",
    }


def test_parts_cost():
    result = asyncio.run(call_business_tool("get_parts_cost", {"model_id": "EWP-12-B", "options": B_OPTIONS}))

    assert result["parts_cost_krw"] == 21650


def test_bom_then_supply_check():
    bom = asyncio.run(call_business_tool("get_bom", {"model_id": "EWP-12-B", "options": B_OPTIONS, "qty": 2000}))
    parts = [{"part_id": p["part_id"], "required_qty": p["required_qty"]} for p in bom["parts"]]

    supply = asyncio.run(call_business_tool("check_parts_supply", {
        "due_date": "2026-12-15", "base_date": "2026-10-19", "parts": parts,
    }))

    mcu = next(p for p in supply["parts"] if p["part_id"] == "P-MCU-G1")
    assert supply["has_risk"] is True
    assert (mcu["status"], mcu["short_qty"], mcu["incoming_delay_days"]) == ("SHORT", 500, 36)
    assert mcu["substitutes"][0]["substitute_part_id"] == "P-MCU-G1-ALT"


def test_tool_error_raises():
    with pytest.raises(McpToolError, match="허용되지 않은 옵션"):
        asyncio.run(call_business_tool("get_bom", {"model_id": "EWP-12-A", "options": ["OPT-IP9K"], "qty": 10}))
