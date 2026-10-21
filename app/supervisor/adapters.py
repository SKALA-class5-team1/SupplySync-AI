"""
상위 그래프용 어댑터(번역기): SG1 → SG2 → SG3 → SG4 사이에서 이름·모양이 다른 값을 바꿔 준다.

SG1~SG3 코드는 고치지 않는다. 모양이 다른 부분은 전부 여기서 맞춘다.
- 순수 함수(입출력이 dict)만 둔다. MCP·LLM 호출은 하지 않아서 테스트가 쉽다.
- 나중에 SG 쪽 모양이 바뀌면 이 파일만 고치면 된다.
"""
from typing import Any, Mapping, Optional

# SG2 대안 id(A~D) → SG3 가 기대하는 type 이름
ALT_TYPE = {"A": "WAIT_INCOMING", "B": "PARTIAL_DELIVERY", "C": "SUBSTITUTE_PART", "D": "NEW_ORDER"}


def sg1_risks_to_sg2(contract_risks: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """SG1 계약 리스크 {item, found, limit, ref, ...} → SG2 ContractRisk {code, description, ref}."""
    out = []
    for risk in contract_risks or []:
        item = str(risk.get("item") or risk.get("code") or "").strip()
        if not item:
            continue
        found, limit = risk.get("found"), risk.get("limit")
        description = risk.get("description") or (
            f"{item}: 확인값 {found}" + (f" / 기준 {limit}" if limit not in (None, "") else "")
        )
        out.append({"code": item, "description": str(description), "ref": risk.get("ref")})
    return out


def bom_to_parts(bom: Mapping[str, Any]) -> list[dict[str, Any]]:
    """get_bom 결과 → SG2 parts [{part_id, required_qty}]."""
    return [{"part_id": p["part_id"], "required_qty": p["required_qty"]} for p in bom.get("parts", [])]


def bom_effective(bom: Mapping[str, Any]) -> list[dict[str, Any]]:
    """get_bom 결과 → SG3 bom_effective [{part_id, part_name, qty_per_unit, source}]."""
    return [
        {
            "part_id": p["part_id"],
            "part_name": p.get("part_name", ""),
            "qty_per_unit": p.get("qty_per_unit"),
            "source": p.get("source", "BASE"),
        }
        for p in bom.get("parts", [])
    ]


def sg1_to_sg2_request(state: Mapping[str, Any], bom: Mapping[str, Any]) -> dict[str, Any]:
    """SG1 출력(state) + BOM → SG2 SupplyAnalysisRequest 에 넣을 dict.

    납기일은 시작품 납품일(demand.sample_due_date)이다. 없으면 SG2 검증에서 오류가 난다.
    """
    demand = state.get("demand") or {}
    request: dict[str, Any] = {
        "due_date": demand.get("sample_due_date") or state.get("due_date"),
        "parts": bom_to_parts(bom),
        "sample_qty": demand.get("sample_qty"),
        "contract_risks": sg1_risks_to_sg2(state.get("contract_risks") or []),
    }
    if state.get("base_date"):
        request["base_date"] = state["base_date"]
    return {k: v for k, v in request.items() if v is not None}


def _alt_to_sg3(alt: Mapping[str, Any], shortage_ids: list[str], recommended_id: Optional[str]) -> dict[str, Any]:
    subs = alt.get("substitutions") or []
    first_sub = subs[0] if subs else {}
    target = first_sub.get("original_part_id") or (shortage_ids[0] if shortage_ids else None)
    covered = sum(s.get("qty", 0) for s in subs) if subs else None
    out: dict[str, Any] = {
        "type": ALT_TYPE[alt["id"]],
        "label": alt.get("name", ""),
        "target_part_id": target,
        "covered_qty": covered,
        "available_date": first_sub.get("usable_date") or alt.get("material_ready_date"),
        "meets_due_date": alt.get("status") == "ON_TIME",
        "delay_days": alt.get("delay_days"),
        "extra_cost_krw": alt.get("extra_cost_krw"),
        "feasible": alt.get("status") != "UNAVAILABLE",
        "recommended": alt["id"] == recommended_id,
        "reason": " / ".join(alt.get("reasons") or []),
        "refs": [s["ref_chunk_id"] for s in subs if s.get("ref_chunk_id")],
    }
    if alt["id"] == "C" and first_sub:
        out.update(
            {
                "substitute_part_id": first_sub.get("substitute_part_id"),
                "substitute_supplier": first_sub.get("supplier"),
                "validation_days": first_sub.get("validation_days"),
                "unit_cost_diff_krw": first_sub.get("unit_cost_diff_krw"),
            }
        )
    return out


def plan_to_dict(plan: list[Mapping[str, Any]]) -> dict[str, int]:
    """SG2 proposed_plan [{part_id, required_qty}] → SG4 final_plan {part_id: qty}."""
    return {p["part_id"]: int(p["required_qty"]) for p in plan or []}


def sg2_result_to_supply_analysis(
    result: Mapping[str, Any], state: Mapping[str, Any], bom: Mapping[str, Any]
) -> dict[str, Any]:
    """SG2 결과(model_dump) → SG3 가 받는 supply_analysis dict (5_sg2_supply.json 모양)."""
    demand = state.get("demand") or {}
    shortages = result.get("shortages") or []
    shortage_ids = [s["part_id"] for s in shortages]
    recommended_id = result.get("recommended_alternative")
    return {
        "rfq_number": state.get("rfq_number", ""),
        "base_date": result.get("base_date"),
        "demand": {"sample_qty": demand.get("sample_qty"), "sample_due_date": demand.get("sample_due_date")},
        "bom_effective": bom_effective(bom),
        "supply_check": result.get("supply_check") or [],
        "shortages": shortages,
        "alternatives": [_alt_to_sg3(a, shortage_ids, recommended_id) for a in result.get("alternatives") or []],
        "recommended_alt": ALT_TYPE.get(recommended_id) if recommended_id else None,
        "final_plan": plan_to_dict(result.get("proposed_plan") or []),
    }


def sg2_result_to_state(
    result: Mapping[str, Any], state: Mapping[str, Any], bom: Mapping[str, Any]
) -> dict[str, Any]:
    """SG2 결과 → 상위 그래프 state 업데이트 (SG3 입력 supply_analysis + SG4 입력 final_plan 등)."""
    analysis = sg2_result_to_supply_analysis(result, state, bom)
    return {
        "supply_analysis": analysis,
        "final_plan": analysis["final_plan"],
        "due_date": result.get("due_date"),
        "base_date": result.get("base_date"),
    }
