"""견적 검증 5항목 (validate_quote)

판정 기준 값은 rules.yaml(policy_limits, quote_margin, quote_format)에서 읽는다.
rule 코드·label은 05_expected/6_sg3_quote.json의 checks와 같다.
"""
from datetime import date, timedelta

from app.quote_drafting.conditions import find_unsupported_numbers

# 문구를 다시 써도 바뀌지 않는 숫자 판정. 실패해도 재작성하지 않고 승인(SG4)에서 판단한다.
POLICY_RULES = {"PRICE", "ANNUAL_CR", "MARGIN"}

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


def check_price(quote: dict, rfq_review: dict) -> dict:
    target = rfq_review["target_price_krw"]
    unit = quote["unit_price_krw"]
    return _check("PRICE", "가격", f"단가 ≤ 목표가 {target:,}원", f"{unit:,}원", unit <= target)


def check_annual_cr(parsed_rfq: dict, rules: dict) -> dict:
    limit = next(l for l in rules["policy_limits"] if l["field"] == "commercial.annual_cr_pct")
    cr = parsed_rfq["commercial"]["annual_cr_pct"]
    return _check("ANNUAL_CR", "연간 인하", f"CR ≤ {limit['max']}%", f"{cr}%", cr <= limit["max"], limit["ref"])


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


def run_checks(quote: dict, condition_facts: list[dict], parsed_rfq: dict, rfq_review: dict, rules: dict) -> list[dict]:
    return [
        check_price(quote, rfq_review),
        check_annual_cr(parsed_rfq, rules),
        check_margin(quote, rules),
        check_price_recalc(quote, condition_facts),
        check_format(quote, rules),
    ]
