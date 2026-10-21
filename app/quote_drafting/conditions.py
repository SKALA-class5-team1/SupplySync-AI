"""견적 조건부 사항(conditions)

코드가 조건의 구조와 숫자(type, field, part_id, qty, extra_cost_krw, ref)를 정하고,
문구(text)는 LLM이 facts만 보고 쓴다. 문구 속 숫자는 facts에 있는 숫자여야 한다.
"""
import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# 대체 부품 조건의 근거 조항: 제20조(사용 사실 명시), 제21조(원가 증가분 별도 표시)
SUBSTITUTE_PART_REF = "제20조, 제21조"

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


@dataclass
class ConditionDraft:
    condition: dict  # quote_draft.conditions 한 항목 (text는 writer가 채움)
    facts: dict  # writer에 넘기는 사실. 문구 숫자 검증 기준
    query: str | None = None  # 근거 문서 검색어
    evidence: list[dict] = field(default_factory=list)


def _get_path(data: dict, path: str):
    for key in path.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def format_number(value) -> str:
    """SG1은 숫자를 float로 줄 수 있다 (22.0, 3.0). 정수면 소수점 없이 쓴다."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _format_rfq_value(value) -> str | None:
    # {"value": 22.0, "unit": "L/h"} → "22 L/h"
    if isinstance(value, dict) and "value" in value:
        return f"{format_number(value['value'])} {value.get('unit') or ''}".strip()
    return None if value is None else format_number(value)


def _field_for_ref(ref: str | None, rules: dict) -> str | None:
    """계약 리스크의 ref로 rules.yaml policy_limits의 필드를 찾는다. SG1(_check_policy_limits)과 같은 방식이다.

    - SG1은 조항(ref)별로 묶고, ref가 없는 한도는 필드 이름을 ref로 쓴다.
    - 한 조항에 필드가 여럿이면 공통 접두어로 묶는다 (warranty_years, warranty_km → contract.warranty).
    - 찾지 못하면 None (조건은 field 없이 만든다).
    """
    fields = [r["field"] for r in rules["policy_limits"] if (r.get("ref") or r["field"]) == ref]
    if not fields:
        return None
    return os.path.commonprefix(fields).rstrip("_.") or fields[0]


def build_conditions(
    parsed_rfq: dict, assumptions: list[dict], contract_risks: list[dict], supply_analysis: dict, rules: dict
) -> list[ConditionDraft]:
    drafts = []

    # 1) SG1 가정값
    for assumption in assumptions:
        drafts.append(
            ConditionDraft(
                condition={"type": "ASSUMPTION", "field": assumption["field"], "text": ""},
                facts={
                    "field": assumption["field"],
                    "assumed_value": assumption["value"],
                    "rfq_value": _format_rfq_value(_get_path(parsed_rfq, assumption["field"])),
                },
            )
        )

    # 2) SG2 추천안이 대체 부품이면 사용 사실과 추가 비용 (다른 대안은 견적 조건이 아니다)
    if supply_analysis.get("recommended_alt") == "SUBSTITUTE_PART":
        selected = supply_analysis["selected_alternative"]
        shortages = {s["part"]: s for s in supply_analysis["shortages"]}
        original_parts = {p["part_id"]: p for p in supply_analysis["bom"]}
        # 후보 목록의 첫 대안이 아니라 SG2가 실제 선택한 모든 대체 부품을 사용한다.
        for alt in selected["parts"]:
            qty = shortages[alt["part_id"]]["short_qty"]
            extra_cost = alt["extra_cost_krw"]
            drafts.append(ConditionDraft(
                condition={
                    "type": "SUBSTITUTE_PART",
                    "text": "",
                    "part_id": alt["substitute_part_id"],
                    "qty": qty,
                    "extra_cost_krw": extra_cost,
                    "ref": SUBSTITUTE_PART_REF,
                },
                facts={
                    "sample_qty": supply_analysis["demand"]["sample_qty"],
                    "qty": qty,
                    "original_part_name": original_parts[alt["part_id"]]["part_name"],
                    "substitute_part_id": alt["substitute_part_id"],
                    "substitute_supplier": alt.get("substitute_supplier"),
                    "extra_cost_krw": extra_cost,
                    "ref": SUBSTITUTE_PART_REF,
                },
                query=SUBSTITUTE_PART_REF.replace(",", ""),
            ))

    # 3) SG1 계약 리스크 → 사내 한도로 수정 제안
    for risk in contract_risks:
        drafts.append(
            ConditionDraft(
                condition={
                    "type": "CONTRACT_PROPOSAL",
                    "field": _field_for_ref(risk.get("ref"), rules),
                    "text": "",
                    "ref": risk.get("ref"),
                },
                facts={
                    "customer_terms": risk["item"],
                    "limit": risk.get("limit"),
                    "suggestion": risk.get("suggestion"),  # SG1이 붙인 수정 제안 (없으면 null)
                    "ref": risk.get("ref"),
                },
                query=risk.get("ref"),
            )
        )

    return drafts


def extract_numbers(text: str) -> set[Decimal]:
    numbers = set()
    for token in _NUMBER.findall(text):
        try:
            numbers.add(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    return numbers


def find_unsupported_numbers(text: str, facts: dict) -> list[str]:
    """문구에 나온 숫자 중 facts(근거 문서 포함)에 없는 숫자."""
    allowed = extract_numbers(json.dumps(facts, ensure_ascii=False))
    return sorted(str(n) for n in extract_numbers(text) - allowed)
