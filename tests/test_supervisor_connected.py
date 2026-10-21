"""실제 SG2·SG3 코드를 어댑터로 이었을 때 모양이 맞는지 확인한다. (SG2/SG3 가 없는 환경에서는 건너뛴다)

외부 호출(MCP, LLM)은 하지 않는다: get_bom 은 가짜 loader, check_parts_supply 는 monkeypatch.
실행: python -m tests.test_supervisor_connected
"""
import asyncio
import json
from pathlib import Path

import yaml

FIX = Path(__file__).resolve().parent / "quote_drafting" / "fixtures"
SUPPLY_PAYLOAD = {
    "due_date": "2026-12-15", "base_date": "2026-10-19", "has_risk": True,
    "parts": [
        {
            "part_id": "P-MCU-G1", "part_name": "제어칩 (고온용)", "supplier": "세온반도체", "is_critical": True,
            "required_qty": 2000, "on_hand": 9000, "allocated": 7500, "available": 1500,
            "short_qty": 500, "status": "SHORT",
            "incoming_qty": 5000, "incoming_date": "2027-01-20", "incoming_before_due": False, "incoming_delay_days": 36,
            "unit_cost_krw": 5300, "lead_time_days": 120, "new_order_arrival": "2027-02-16",
            "risks": ["SHORTAGE", "INCOMING_AFTER_DUE", "NEW_ORDER_AFTER_DUE"],
            "substitutes": [{
                "substitute_part_id": "P-MCU-G1-ALT", "part_name": "제어칩 (고온용, 대체)", "supplier": "누리칩스",
                "available": 3000, "covers_shortage": True, "unit_cost_krw": 5600, "unit_cost_diff_krw": 300,
                "validation_days": 14, "usable_date": "2026-11-02", "lead_time_days": 30,
                "pin_compatible": True, "ref_chunk_id": "alt-002",
            }],
        },
        {
            "part_id": "P-MTR-BL12", "part_name": "모터", "supplier": "한결모터", "is_critical": True,
            "required_qty": 2000, "on_hand": 18000, "allocated": 6000, "available": 12000,
            "short_qty": 0, "status": "OK",
            "incoming_qty": 10000, "incoming_date": "2026-11-20", "incoming_before_due": True, "incoming_delay_days": 0,
            "unit_cost_krw": 9500, "lead_time_days": 45, "new_order_arrival": "2026-12-03", "risks": [], "substitutes": [],
        },
    ],
}
BOM = {"parts": [
    {"part_id": "P-MTR-BL12", "part_name": "모터", "qty_per_unit": 1, "required_qty": 2000, "source": "BASE"},
    {"part_id": "P-MCU-G1", "part_name": "제어칩 (고온용)", "qty_per_unit": 1, "required_qty": 2000, "source": "BASE"},
]}


def test_real_sg2_to_sg3_shape() -> None:
    from app.quote_drafting.conditions import build_conditions
    from app.quote_drafting.schemas import SG3State
    from app.supervisor.graph import SupervisorState
    from app.supervisor.real_nodes import make_sg2_node
    from app.supply_analysis import agent
    from app.supply_analysis.schemas import SupplyCheckResult

    async def fake_check(request):  # MCP check_parts_supply 대신
        by_id = {p["part_id"]: p for p in SUPPLY_PAYLOAD["parts"]}
        full = {**SUPPLY_PAYLOAD, "parts": [by_id[p.part_id] for p in request.parts]}
        return SupplyCheckResult.model_validate(full)

    async def fake_bom(state):
        return BOM

    agent.check_supply = fake_check  # 모듈 전역을 바꿔 끼운다 (이 테스트 프로세스 안에서만)
    sg1 = json.loads((FIX / "4_output_state.json").read_text(encoding="utf-8"))
    state = SupervisorState(thread_id="t-1", rfq_id=1, **{k: sg1[k] for k in (
        "rfq_number", "parsed_rfq", "selected_model", "options", "assumptions", "contract_risks", "demand") if k in sg1})
    out = asyncio.run(make_sg2_node(fake_bom)(state))

    sa = out["supply_analysis"]
    assert out["final_plan"] == {"P-MTR-BL12": 2000, "P-MCU-G1": 1500, "P-MCU-G1-ALT": 500}, out["final_plan"]
    assert out["due_date"] == "2026-12-15" and out["base_date"] == "2026-10-19"
    assert sa["recommended_alt"] == "SUBSTITUTE_PART"
    assert [a["type"] for a in sa["alternatives"]] == ["WAIT_INCOMING", "PARTIAL_DELIVERY", "SUBSTITUTE_PART", "NEW_ORDER"]
    sub = next(a for a in sa["alternatives"] if a["type"] == "SUBSTITUTE_PART")
    assert sub["substitute_part_id"] == "P-MCU-G1-ALT" and sub["target_part_id"] == "P-MCU-G1"
    assert sub["covered_qty"] == 500 and sub["extra_cost_krw"] == 150000 and sub["refs"] == ["alt-002"]
    assert sa["shortages"][0]["part_id"] == "P-MCU-G1"

    # SG3 가 이 입력을 그대로 받아 조건 초안을 만들 수 있다
    SG3State(**{**state.model_dump(), **out})
    rules = yaml.safe_load((FIX / "rules_sg3.yaml").read_text(encoding="utf-8"))
    drafts = build_conditions(state.parsed_rfq, state.assumptions, state.contract_risks, sa, rules)
    assert drafts, "SG3 조건 초안이 비어 있음"
    print("OK: 실제 SG2 결과가 어댑터를 거쳐 SG3 입력(SG3State·build_conditions)으로 통과")


def test_connected_graph_builds() -> None:
    from app.supervisor.graph import build_connected_supervisor_graph

    graph = build_connected_supervisor_graph().compile()
    assert {"sg1_rfq_review", "sg2_supply_analysis", "sg3_quote_drafting", "sg4_approval_registration"} <= set(
        graph.get_graph().nodes
    )
    print("OK: 실제 SG1·SG2·SG3·SG4 를 붙인 상위 그래프가 만들어짐")


if __name__ == "__main__":
    try:
        import app.quote_drafting.graph  # noqa: F401
        import app.supply_analysis.service  # noqa: F401
    except ImportError as exc:
        print(f"SKIP: SG2/SG3 코드가 이 브랜치에 아직 없어 건너뜀 ({exc})")
    else:
        test_real_sg2_to_sg3_shape()
        test_connected_graph_builds()
