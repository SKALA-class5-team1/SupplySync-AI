"""
견적 계산 순수 함수

숫자는 코드가 계산하고 LLM은 문구만 쓴다. 이 모듈은 core, MCP, DB에 의존하지 않는다.
공식 근거: dataset/04_rules/rules.yaml (price_by_year, quote_margin, quote_format)
"""
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Mapping, Sequence


def _round_half_up(value: Decimal, digits: int = 0) -> Decimal:
    """float 오차와 은행가 반올림을 피하고 항상 반올림(half up)한다."""
    return value.quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def calc_price_breakdown(
    model_id: str,
    base_price_krw: int,
    option_prices: Mapping[str, int],
    selected_options: Sequence[str],
) -> list[dict]:
    """모델 기본가와 선택된 옵션가만 항목별로 나열한다.

    option_prices는 모델이 지원하는 전체 옵션 {option_code: add_price_krw}.
    선택되지 않은 옵션은 합산하지 않고, 지원하지 않는 옵션이 선택되면 ValueError.
    """
    unknown = [code for code in selected_options if code not in option_prices]
    if unknown:
        raise ValueError(f"{model_id}에서 지원하지 않는 옵션: {unknown}")

    breakdown = [{"code": model_id, "amount_krw": base_price_krw}]
    breakdown += [{"code": code, "amount_krw": option_prices[code]} for code in selected_options]
    return breakdown


def calc_unit_price(price_breakdown: Sequence[Mapping]) -> int:
    """단가 = 모델 기본가 + 선택 옵션가 합계."""
    return sum(item["amount_krw"] for item in price_breakdown)


def calc_margin_pct(unit_price_krw: int, parts_cost_krw: int) -> float:
    """부품 원가 기준 마진(%) = (단가 - 부품 원가) / 단가, 소수 첫째 자리 반올림."""
    if unit_price_krw <= 0:
        raise ValueError("단가는 0보다 커야 합니다.")
    margin = (Decimal(unit_price_krw) - Decimal(parts_cost_krw)) / Decimal(unit_price_krw) * 100
    return float(_round_half_up(margin, 1))


def calc_price_by_year(
    unit_price_krw: int,
    parts_cost_krw: int,
    annual_cr_pct: float,
    years: int,
) -> list[dict]:
    """연도별 단가와 마진.

    단가 = round(unit_price * (1 - cr/100) ** (year - 1)), 원 단위 반올림.
    1년차는 기준가이고 매년 반올림값을 누적하지 않는다. cr_applied는 인하 적용 횟수(year - 1).
    """
    if years < 1:
        raise ValueError("공급 기간은 1년 이상이어야 합니다.")

    factor = Decimal(1) - Decimal(str(annual_cr_pct)) / 100
    rows = []
    for year in range(1, years + 1):
        price = int(_round_half_up(Decimal(unit_price_krw) * factor ** (year - 1)))
        rows.append(
            {
                "year": year,
                "cr_applied": year - 1,
                "unit_price_krw": price,
                "margin_pct": calc_margin_pct(price, parts_cost_krw),
            }
        )
    return rows


def calc_valid_until(issue_date: date, valid_days: int = 30) -> date:
    """유효기간 = 발행일 + valid_days (rules.yaml quote_format.valid_days)."""
    return issue_date + timedelta(days=valid_days)


def calc_substitute_extra_cost(qty: int, unit_cost_diff_krw: int) -> int:
    """대체 부품 추가 비용 = 수량 × 단가 차이. 단가·원가에는 넣지 않고 conditions에만 표시한다 (제21조)."""
    return qty * unit_cost_diff_krw
