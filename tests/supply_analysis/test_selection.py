import asyncio
from copy import deepcopy

from app.supply_analysis.rules import read_criteria
from app.supply_analysis.schemas import DocHit

from app.supply_analysis.service import analyze_supply


def run(data):
    return asyncio.run(analyze_supply(data))


def add_second_shortage(gateway, part_id="P-MTR-BL12", own_candidate=False):
    """독립된 원 부품이 같은 대체 재고를 쓸 수 있는 가상의 승인 문서 시나리오."""
    part = next(p for p in gateway.supply["parts"] if p["part_id"] == part_id)
    part.update(available=1500, short_qty=500, status="SHORT")
    result = deepcopy(gateway.substitutes["P-MCU-G1"])
    result["part_id"] = part_id
    candidate = result["substitutes"][0]
    candidate["ref_chunk_id"] = "alt-second"
    doc = deepcopy(gateway.alt_docs[1])
    doc["chunk_id"] = "alt-second"
    doc["content"] = doc["content"].replace("(P-MCU-G1)", f"({part_id})")
    if own_candidate:
        candidate["substitute_part_id"] = "P-SECOND-ALT"
        doc["content"] = doc["content"].replace("P-MCU-G1-ALT", "P-SECOND-ALT")
    gateway.alt_docs.append(doc)
    gateway.substitutes[part_id] = result


def test_multiple_shortages_accumulate_shared_substitute_quantities(gateway, request_data, bom_plan):
    add_second_shortage(gateway)
    result = run(request_data)
    assert result.saved
    assert len(result.alternatives) == 8
    assert result.final_plan == {**bom_plan, "P-MCU-G1": 1500, "P-MTR-BL12": 1500, "P-MCU-G1-ALT": 1000}
    assert result.selected_alternative.extra_cost_krw == 300000


def test_shared_stock_not_double_allocated(gateway, request_data):
    gateway.substitute["available"] = 700
    add_second_shortage(gateway)
    result = run(request_data)
    assert result.next_step == "hold"
    assert result.saved and result.status == "ON_HOLD"
    assert result.final_plan == {}


def test_global_candidate_assignment_uses_second_choice_to_preserve_stock(gateway, request_data, bom_plan):
    gateway.substitute["available"] = 500
    add_second_shortage(gateway)
    alternative = deepcopy(gateway.substitute)
    alternative.update(substitute_part_id="P-OTHER-ALT", ref_chunk_id="alt-other", unit_cost_krw=5700, unit_cost_diff_krw=400)
    gateway.substitutes["P-MCU-G1"]["substitutes"].append(alternative)
    doc = deepcopy(gateway.alt_docs[1])
    doc["chunk_id"] = "alt-other"
    doc["content"] = doc["content"].replace("P-MCU-G1-ALT", "P-OTHER-ALT").replace("300원", "400원")
    gateway.alt_docs.append(doc)
    result = run(request_data)
    assert result.saved
    assert result.final_plan == {**bom_plan, "P-MCU-G1": 1500, "P-MTR-BL12": 1500, "P-MCU-G1-ALT": 500, "P-OTHER-ALT": 500}
    assert result.selected_alternative.extra_cost_krw == 350000


def test_substitute_stock_already_needed_by_bom_is_reserved(gateway, request_data):
    gateway.substitute["available"] = 2200
    gateway.bom["parts"].append({"part_id": "P-MCU-G1-ALT", "part_name": "기본 BOM 공유 부품", "qty_per_unit": 1, "required_qty": 2000, "source": "BASE"})
    gateway.supply["parts"].append({"part_id": "P-MCU-G1-ALT", "required_qty": 2000, "available": 2200, "short_qty": 0, "status": "OK"})
    result = run(request_data)
    assert result.next_step == "hold"
    assert result.final_plan == {}


def test_full_substitution_omits_original_and_preserves_other_parts(gateway, request_data, bom_plan):
    gateway.chip.update(available=0, short_qty=2000)
    result = run(request_data)
    expected = {**bom_plan, "P-MCU-G1-ALT": 2000}
    del expected["P-MCU-G1"]
    assert result.final_plan == expected
    assert result.selected_alternative.final_plan == expected
    assert gateway.calls[-1][1]["final_plan"] == expected
    assert {p.part_id: p.required_qty for p in result.bom} == bom_plan


def test_rag_order_controls_cost_vs_validation_tie_break(gateway, request_data, bom_plan):
    second = deepcopy(gateway.substitute)
    second.update(substitute_part_id="P-FAST-ALT", ref_chunk_id="alt-fast", unit_cost_krw=5700, unit_cost_diff_krw=400)
    gateway.substitute.update(validation_days=21, usable_date="2026-11-09")
    gateway.substitutes["P-MCU-G1"]["substitutes"].append(second)
    doc = deepcopy(gateway.alt_docs[1])
    doc["chunk_id"] = "alt-fast"
    doc["content"] = doc["content"].replace("P-MCU-G1-ALT", "P-FAST-ALT")
    gateway.alt_docs.append(doc)
    assert run(request_data).final_plan["P-MCU-G1-ALT"] == 500
    gateway.alt_docs[2]["content"] = gateway.alt_docs[2]["content"].replace("추가 원가, 검증 기간", "검증 기간, 추가 원가")
    result = run(request_data)
    assert result.final_plan == {**bom_plan, "P-MCU-G1": 1500, "P-FAST-ALT": 500}
    assert result.selected_alternative.extra_cost_krw == 200000


def test_rule_order_is_read_from_document_not_chunk_id(gateway):
    gateway.alt_docs[2]["chunk_id"] = "new-import-id"
    criteria = read_criteria([DocHit.model_validate(d) for d in gateway.alt_docs])
    assert criteria.order == ["meets_due_date", "extra_cost_krw", "validation_days", "quality_risk"]
