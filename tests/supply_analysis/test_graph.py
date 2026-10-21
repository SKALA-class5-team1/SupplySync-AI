import asyncio
from copy import deepcopy

import pytest

from app.supply_analysis.mcp_tools import ToolError
from app.supply_analysis.service import analyze_supply, supply_analysis


def run(data):
    return asyncio.run(analyze_supply(data))


def test_demo_rag_evidence_and_persisted_sg2_plan(gateway, request_data, bom_plan, substitute_plan):
    result = run(request_data)
    assert result.error is None
    assert result.next_step == "quote_drafting"
    assert result.saved
    assert result.recommended_alt == "SUBSTITUTE_PART"
    assert result.final_plan == substitute_plan
    assert result.selected_alternative.final_plan == substitute_plan
    assert {p.part_id: p.required_qty for p in result.bom} == bom_plan
    assert result.bom_effective == result.bom
    assert result.selected_alternative.extra_cost_krw == 150000
    assert result.selected_alternative.parts[0].usable_date.isoformat() == "2026-11-02"
    assert len(result.bom) == 6
    assert len(result.alternatives) == 4
    assert result.shortages[0].model_dump() == {"part": "P-MCU-G1", "needed": 2000, "available": 1500, "short_qty": 500, "eta_days": 93}
    refs = {e.chunk_id for e in result.selected_alternative.parts[0].evidence}
    assert refs == {"alt-001", "alt-002", "alt-003", "pol-009", "pol-010"}
    wait, partial, _, order = result.alternatives
    assert wait.delay_days == 36
    assert not partial.feasible
    assert (partial.on_time_qty, partial.delayed_qty) == (1500, 500)
    assert "지연 배상" in partial.constraints[0]
    assert order.delay_days == 33
    saved = gateway.calls[-1]
    assert saved[0] == "business_save_review"
    assert saved[1]["stage"] == "SG2"
    assert saved[1]["status"] == "DONE"
    assert saved[1]["final_plan"] == result.final_plan
    assert not {"selected_model", "options", "issues"} & saved[1].keys()
    assert all(value is not None for value in saved[1].values())
    calls = [arguments for name, arguments in gateway.calls if name == "business_get_substitutes"]
    assert calls == [{"part_id": "P-MCU-G1", "short_qty": 500, "base_date": "2026-10-19"}]


def test_no_shortage_skips_rag_and_clears_previous_output(gateway, request_data, bom_plan):
    gateway.chip.update(available=2000, short_qty=0, status="OK")
    gateway.supply["has_risk"] = False
    request_data.update(final_plan={"STALE": 500}, recommended_alt="SUBSTITUTE_PART", error="old error")
    result = run(request_data)
    assert result.next_step == "quote_drafting"
    assert result.final_plan == bom_plan
    assert result.recommended_alt is None
    assert result.alternatives == []
    assert [name for name, _ in gateway.calls] == ["business_get_bom", "business_check_parts_supply", "business_save_review"]
    assert gateway.calls[-1][1]["final_plan"] == bom_plan
    assert "recommended_alt" not in gateway.calls[-1][1]


def test_common_state_inputs_and_output_keys(gateway, request_data, substitute_plan):
    shared = {
        "thread_id": request_data["thread_id"],
        "requirements": {key: request_data[key] for key in ("rfq_number", "demand", "contract_risks", "base_date")},
        "matched_product": {"model_id": request_data["selected_model"], "options": request_data["options"]},
        "feedback": {"reason": "재검토"},
    }
    result = asyncio.run(supply_analysis(shared))
    assert result["next_step"] == "quote_drafting"
    assert result["final_plan"] == substitute_plan
    assert {"bom", "supply_check", "alternatives", "selected_alternative"} <= result.keys()
    assert not {"request", "candidates", "requirements", "matched_product"} & result.keys()


@pytest.mark.parametrize("field,value", [
    ("pin_compatible", False), ("available", 499), ("covers_shortage", False),
    ("validation_days", 7), ("usable_date", "2026-10-25"),
    ("ref_chunk_id", "missing"), ("unit_cost_diff_krw", 200),
])
def test_invalid_substitute_never_recommended(gateway, request_data, field, value):
    gateway.substitute[field] = value
    result = run(request_data)
    assert result.next_step == "hold"
    assert result.saved and result.status == "ON_HOLD"
    assert result.final_plan == {}
    assert not next(a for a in result.alternatives if a.type == "SUBSTITUTE_PART").feasible
    assert gateway.calls[-1][0] == "business_save_review"
    assert gateway.calls[-1][1]["final_plan"] == {}


@pytest.mark.parametrize("change", ["empty", "no_rank", "changed_rank", "wrong_grade", "denied", "no_policy"])
def test_missing_or_changed_rag_blocks_unsupported_recommendation(gateway, request_data, change):
    if change == "empty":
        gateway.alt_docs = []
    elif change == "no_rank":
        gateway.alt_docs = gateway.alt_docs[:2]
    elif change == "changed_rank":
        gateway.alt_docs[2]["content"] = "추가 원가가 가장 중요하다."
    elif change == "wrong_grade":
        gateway.alt_docs[1]["content"] = gateway.alt_docs[1]["content"].replace("같은 고온 등급", "낮은 성능 등급")
    elif change == "denied":
        gateway.alt_docs[1]["content"] += " 이 부품의 사용은 금지한다."
    else:
        gateway.policy_docs = []
    result = run(request_data)
    assert result.next_step == "hold"
    assert result.final_plan == {}
    assert result.saved and result.status == "ON_HOLD"
    assert gateway.calls[-1][1]["final_plan"] == {}


def test_incoming_on_due_wins_and_substitution_is_ineligible(gateway, request_data, bom_plan):
    gateway.chip.update(incoming_date="2026-12-15", incoming_before_due=True, incoming_delay_days=0)
    result = run(request_data)
    assert result.recommended_alt == "WAIT_INCOMING"
    assert result.selected_alternative.extra_cost_krw == 0
    assert result.final_plan == bom_plan
    assert result.selected_alternative.final_plan == bom_plan
    assert gateway.calls[-1][1]["final_plan"] == bom_plan
    assert not next(a for a in result.alternatives if a.type == "SUBSTITUTE_PART").feasible


def test_empty_substitutes_and_short_incoming_are_not_assumed_feasible(gateway, request_data):
    gateway.substitutes["P-MCU-G1"]["substitutes"] = []
    gateway.chip["incoming_qty"] = 100
    gateway.chip["new_order_arrival"] = None
    result = run(request_data)
    assert result.next_step == "hold"
    assert len(result.alternatives) == 4
    assert not any(alt.feasible for alt in result.alternatives)


def test_new_order_arrives_on_due(gateway, request_data, bom_plan):
    gateway.chip["new_order_arrival"] = "2026-12-15"
    result = run(request_data)
    assert result.recommended_alt == "NEW_ORDER"
    assert result.final_plan == bom_plan
    assert result.selected_alternative.final_plan == bom_plan
    assert gateway.calls[-1][1]["final_plan"] == bom_plan


def test_full_plan_preserves_required_quantity_for_unchanged_multi_unit_part(gateway, request_data, substitute_plan):
    motor = next(p for p in gateway.bom["parts"] if p["part_id"] == "P-MTR-BL12")
    motor.update(qty_per_unit=2, required_qty=4000)
    next(p for p in gateway.supply["parts"] if p["part_id"] == "P-MTR-BL12")["required_qty"] = 4000
    result = run(request_data)
    assert result.saved
    assert result.final_plan == {**substitute_plan, "P-MTR-BL12": 4000}
    assert gateway.calls[-1][1]["final_plan"] == result.final_plan


def test_base_date_omitted_is_reused_from_supply_tool(gateway, request_data):
    del request_data["base_date"]
    result = run(request_data)
    assert result.saved
    assert "base_date" not in next(args for name, args in gateway.calls if name == "business_check_parts_supply")
    assert next(args for name, args in gateway.calls if name == "business_get_substitutes")["base_date"] == "2026-10-19"


@pytest.mark.parametrize("tool", ["business_get_bom", "business_check_parts_supply", "rag_search_docs", "business_get_substitutes", "business_save_review"])
def test_tool_failure_returns_failed_and_no_committed_plan(gateway, request_data, tool):
    gateway.failures[tool] = ToolError("서비스 장애")
    result = run(request_data)
    assert result.next_step == "hold"
    assert result.status == "FAILED"
    assert "서비스 장애" in result.error
    assert not result.saved
    assert result.final_plan == {}


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "wrong_qty", "wrong_short", "wrong_date"])
def test_inconsistent_supply_does_not_progress(gateway, request_data, corruption):
    if corruption == "missing":
        gateway.supply["parts"].pop()
    elif corruption == "duplicate":
        gateway.supply["parts"][0] = deepcopy(gateway.supply["parts"][1])
    elif corruption == "wrong_qty":
        gateway.chip.update(required_qty=3000, short_qty=1500)
    elif corruption == "wrong_short":
        gateway.chip["short_qty"] = 0
    else:
        gateway.chip["incoming_delay_days"] = 1
    result = run(request_data)
    assert result.next_step == "hold"
    assert not any(name == "business_save_review" for name, _ in gateway.calls)


def test_invalid_request_does_not_call_mcp(gateway, request_data):
    request_data["demand"]["sample_qty"] = 0
    assert run(request_data).next_step == "hold"
    assert gateway.calls == []


def test_conflicting_common_and_flat_selection_is_rejected(gateway, request_data):
    request_data["matched_product"] = {"model_id": "DIFFERENT"}
    assert run(request_data).next_step == "hold"
    assert gateway.calls == []
