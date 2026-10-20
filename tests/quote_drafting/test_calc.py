"""calc.py 단위 테스트. 기대값은 dataset/05_expected/6_sg3_quote.json과 대조한다."""
import json
from datetime import date
from pathlib import Path

import pytest

from app.quote_drafting.calc import (
    calc_margin_pct,
    calc_price_breakdown,
    calc_price_by_year,
    calc_substitute_extra_cost,
    calc_unit_price,
    calc_valid_until,
)

EXPECTED_PATH = Path(__file__).resolve().parents[2] / "dataset" / "05_expected" / "6_sg3_quote.json"
EXPECTED = json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))["quote_draft"]

OPTION_PRICES = {"OPT-HT": 2400, "OPT-IP9K": 900, "OPT-UNUSED": 1500}
PARTS_COST = 21650


def test_price_breakdown_matches_expected():
    breakdown = calc_price_breakdown("EWP-12-B", 34000, OPTION_PRICES, ["OPT-HT", "OPT-IP9K"])
    assert breakdown == EXPECTED["price_breakdown"]


def test_price_breakdown_ignores_unselected_options():
    breakdown = calc_price_breakdown("EWP-12-B", 34000, OPTION_PRICES, ["OPT-HT"])
    assert [item["code"] for item in breakdown] == ["EWP-12-B", "OPT-HT"]


def test_price_breakdown_rejects_unsupported_option():
    with pytest.raises(ValueError):
        calc_price_breakdown("EWP-12-B", 34000, OPTION_PRICES, ["OPT-NONE"])


def test_unit_price_matches_expected():
    breakdown = calc_price_breakdown("EWP-12-B", 34000, OPTION_PRICES, ["OPT-HT", "OPT-IP9K"])
    assert calc_unit_price(breakdown) == EXPECTED["unit_price_krw"] == 37300


def test_margin_matches_expected():
    assert calc_margin_pct(37300, PARTS_COST) == EXPECTED["margin_pct"] == 42.0


def test_margin_rejects_non_positive_price():
    with pytest.raises(ValueError):
        calc_margin_pct(0, PARTS_COST)


def test_price_by_year_matches_expected():
    assert calc_price_by_year(37300, PARTS_COST, 3, 6) == EXPECTED["price_by_year"]


def test_price_by_year_does_not_compound_rounding():
    # 매년 반올림값을 누적하면 5년차가 33022가 되므로, 원값 기준 33021과 구분된다.
    rows = calc_price_by_year(37300, PARTS_COST, 3, 6)
    assert rows[4]["unit_price_krw"] == 33021


def test_price_by_year_last_year_margin_above_floor():
    last = calc_price_by_year(37300, PARTS_COST, 3, 6)[-1]
    assert last["margin_pct"] == 32.4 >= 30


def test_price_by_year_zero_cr_keeps_price():
    rows = calc_price_by_year(37300, PARTS_COST, 0, 3)
    assert {row["unit_price_krw"] for row in rows} == {37300}


def test_price_by_year_rejects_zero_years():
    with pytest.raises(ValueError):
        calc_price_by_year(37300, PARTS_COST, 3, 0)


def test_valid_until_matches_expected():
    issue = date.fromisoformat(EXPECTED["issue_date"])
    assert calc_valid_until(issue).isoformat() == EXPECTED["valid_until"] == "2026-11-18"


def test_substitute_extra_cost_matches_expected():
    substitute = next(c for c in EXPECTED["conditions"] if c["type"] == "SUBSTITUTE_PART")
    assert calc_substitute_extra_cost(500, 300) == substitute["extra_cost_krw"] == 150000
