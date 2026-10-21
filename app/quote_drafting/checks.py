"""견적 검증 5항목 (validate_quote)

판정 기준 값은 rules.yaml(policy_limits, quote_margin, quote_format)에서 읽는다.
rule 코드·label은 05_expected/6_sg3_quote.json의 checks와 같다.
"""
from datetime import date, timedelta

from app.quote_drafting.conditions import find_unsupported_numbers, format_number

# 문구를 다시 써도 바뀌지 않는 숫자 판정. 실패해도 재작성하지 않고 승인(SG4)에서 판단한다.
POLICY_RULES = {"PRICE", "ANNUAL_CR", "MARGIN"}


def annual_cr_pct(parsed_rfq: dict) -> float:
    """연간 인하율(%). SG1이 값을 못 찾으면(null) 고객 요구가 없는 것으로 보고 0."""
    cr = (parsed_rfq.get("commercial") or {}).get("annual_cr_pct")
    return 0 if cr is None else cr


def needs_rewrite(checks: list[dict], conditions: list[dict]) -> list[dict]:
    """문구를 다시 쓰면 고칠 수 있는 실패만 고른다: 문구 속 숫자 불일치, 빈 문구.

    필수 데이터 누락(FORMAT)이나 숫자 판정(POLICY_RULES)은 문구로 못 고치므로 승인 단계로 넘긴다.
    """
    empty_text = any(not c.get("text", "").strip() for c in conditions)
    return [
        c for c in checks
        if c["result"] == "FAIL" and (c["rule"] == "PRICE_RECALC" or (c["rule"] == "FORMAT" and empty_text))
    ]

# quote_format.required 항목 → quote_draft 필드
_FORMAT_FIELDS = {
    "recipient": "recipient",
    "item": "item",
    "unit_price": "unit_price_krw",
    "quantity": "eau",
    "due_date": "sample_due_date",
    "incoterms": "incoterms",
    "valid_until": "valid_until",
    "conditions": "conditions",
}


def _check(rule: str, label: str, criterion: str, value: str, passed: bool, ref: str | None = None) -> dict:
    result = {"rule": rule, "label": label, "criterion": criterion, "value": value, "result": "PASS" if passed else "FAIL"}
    if ref:
        result["ref"] = ref
    return result


def check_price(quote: dict, target: int | None) -> dict:
    unit = quote["unit_price_krw"]
    if target is None:
        # SG1이 목표가를 못 찾은 경우. 비교할 수 없어 실패로 두고 승인 단계에서 판단한다.
        return _check("PRICE", "가격", "단가 ≤ 목표가", f"{unit:,}원 (목표가 없음)", False)
    return _check("PRICE", "가격", f"단가 ≤ 목표가 {target:,}원", f"{unit:,}원", unit <= target)


def check_annual_cr(parsed_rfq: dict, rules: dict) -> dict:
    limit = next(l for l in rules["policy_limits"] if l["field"] == "commercial.annual_cr_pct")
    cr = annual_cr_pct(parsed_rfq)
    return _check(
        "ANNUAL_CR", "연간 인하", f"CR ≤ {format_number(limit['max'])}%", f"{format_number(cr)}%",
        cr <= limit["max"], limit["ref"],
    )


def check_margin(quote: dict, rules: dict) -> dict:
    qm = rules["quote_margin"]
    rows = quote["price_by_year"]
    years = [rows[-1]["year"] if y == "last" else y for y in qm["check_years"]]
    margins = [next(r["margin_pct"] for r in rows if r["year"] == y) for y in years]
    return _check(
        "MARGIN",
        "마진",
        f"{qm['basis']} 기준 {qm['min_pct']}% 이상 ({'·'.join(f'{y}년차' for y in years)})",
        " / ".join(f"{m:.1f}%" for m in margins),
        all(m >= qm["min_pct"] for m in margins),
        qm["ref"],
    )


def check_price_recalc(quote: dict, condition_facts: list[dict]) -> dict:
    amounts = [item["amount_krw"] for item in quote["price_breakdown"]]
    unit = quote["unit_price_krw"]
    problems = []
    if sum(amounts) != unit or quote["price_by_year"][0]["unit_price_krw"] != unit:
        problems.append("단가 합계 불일치")
    for condition, facts in zip(quote["conditions"], condition_facts):
        unsupported = find_unsupported_numbers(condition["text"], facts)
        if unsupported:
            problems.append(f"{condition['type']} 문구에 근거 없는 숫자 {unsupported}: {condition['text']}")

    value = f"{' + '.join(f'{a:,}' for a in amounts)} = {unit:,}"
    if problems:
        value += " / " + "; ".join(problems)
    return _check("PRICE_RECALC", "수치 일치", "단가 = 모델가 + 옵션가 합계", value, not problems)


def check_format(quote: dict, rules: dict) -> dict:
    qf = rules["quote_format"]
    # 조건부 사항이 없는 견적도 있으므로 conditions의 빈 목록은 누락이 아니다
    problems = [name for name in qf["required"] if quote.get(_FORMAT_FIELDS[name]) in (None, "")]
    if problems:
        problems = [f"누락: {', '.join(problems)}"]
    expected_until = date.fromisoformat(quote["issue_date"]) + timedelta(days=qf["valid_days"])
    if quote.get("valid_until") != expected_until.isoformat():
        problems.append(f"유효기간이 발행일 + {qf['valid_days']}일({expected_until})과 다름")
    if any(not c.get("text", "").strip() for c in quote.get("conditions", [])):
        problems.append("문구가 빈 조건 있음")
    return _check(
        "FORMAT",
        "문서 형식",
        f"필수 {len(qf['required'])}항목, 유효기간 발행일 + {qf['valid_days']}일",
        "; ".join(problems) or "누락 없음",
        not problems,
        qf["ref"],
    )


def run_checks(
    quote: dict, condition_facts: list[dict], parsed_rfq: dict, target_price_krw: int, rules: dict
) -> list[dict]:
    return [
        check_price(quote, target_price_krw),
        check_annual_cr(parsed_rfq, rules),
        check_margin(quote, rules),
        check_price_recalc(quote, condition_facts),
        check_format(quote, rules),
    ]
