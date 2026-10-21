"""최종 계획이 SG3에서 delta로 해석되거나 미선택 후보로 바뀌지 않는지 확인한다."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.core.rules import load_rules
from app.quote_drafting.conditions import build_conditions
from app.supply_analysis.plan import validate_final_plan
from app.supply_analysis.schemas import SupplyAnalysisResult


def supply():
    path = Path(__file__).parents[1] / "quote_drafting/fixtures/5_sg2_supply.json"
    return json.loads(path.read_text())


@pytest.mark.parametrize("change", ["delta", "quantity", "selected", "missing_alternative", "unsaved"])
def test_handoff_rejects_incomplete_or_inconsistent_plan(change):
    data = supply()
    if change == "delta":
        del data["final_plan"]["P-CON-4P"]
        data["selected_alternative"]["final_plan"] = data["final_plan"]
    elif change == "quantity":
        data["final_plan"]["P-MCU-G1-ALT"] += 1
        data["selected_alternative"]["final_plan"] = data["final_plan"]
    elif change == "selected":
        data["selected_alternative"]["final_plan"] = {}
    elif change == "missing_alternative":
        data["selected_alternative"]["parts"] = []
    else:
        data["saved"] = False
    with pytest.raises(ValueError):
        validate_final_plan(SupplyAnalysisResult.model_validate(data))


def test_all_selected_substitutions_become_quote_conditions():
    data = supply()
    second = deepcopy(data["selected_alternative"]["parts"][0])
    second.update(part_id="P-HSG-AL", substitute_part_id="P-HSG-ALT", extra_cost_krw=20000)
    data["shortages"].append({"part": "P-HSG-AL", "needed": 2000, "available": 1800, "short_qty": 200, "eta_days": 90})
    data["selected_alternative"]["parts"].append(second)
    data["final_plan"].update({"P-HSG-AL": 1800, "P-HSG-ALT": 200})
    data["selected_alternative"]["final_plan"] = data["final_plan"]
    # 목록의 첫 대체 후보를 선택하면 아래의 존재하지 않는 부품이 견적에 섞인다.
    data["alternatives"].insert(0, {**second, "substitute_part_id": "NOT-SELECTED"})
    validate_final_plan(SupplyAnalysisResult.model_validate(data))
    conditions = build_conditions({}, [], [], data, load_rules())
    assert [(c.condition["part_id"], c.condition["qty"], c.condition["extra_cost_krw"]) for c in conditions] == [
        ("P-MCU-G1-ALT", 500, 150000), ("P-HSG-ALT", 200, 20000),
    ]
