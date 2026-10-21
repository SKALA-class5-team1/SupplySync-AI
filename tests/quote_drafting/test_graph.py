"""SG3 서브그래프 테스트. mock 데이터 소스 + 고정 문구로 05_expected/6_sg3_quote.json과 대조한다."""
import asyncio
import json
from pathlib import Path

import pytest

from app.core.rules import RULES_PATH
from app.quote_drafting.conditions import find_unsupported_numbers
from app.quote_drafting.data_source import MockQuoteDataSource
from app.quote_drafting.graph import build_quote_drafting_graph
from app.quote_drafting.nodes import MAX_ATTEMPTS
from app.quote_drafting.writer import TemplateConditionWriter

pytestmark = pytest.mark.skipif(not RULES_PATH.exists(), reason=f"rules.yaml 없음: {RULES_PATH}")

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


EXPECTED = _load("6_sg3_quote")["quote_draft"]


def _input() -> dict:
    return {
        "thread_id": "thread-1",
        "review_id": 1,
        # SG1 parse_rfq는 아직 발행사(부서)를 뽑지 않아 테스트 입력에만 넣는다
        "parsed_rfq": {**_load("1_parsed_rfq"), "issuer": "한별모터스 구매본부"},
        "rfq_review": _load("4_output_state"),
        "supply_analysis": _load("5_sg2_supply"),
        "issue_date": "2026-10-19",
    }


def _run(source, writer=None, **overrides) -> dict:
    graph = build_quote_drafting_graph(source, writer or TemplateConditionWriter())
    return asyncio.run(graph.ainvoke({**_input(), **overrides}))


def _without_text(conditions: list[dict]) -> list[dict]:
    return [{k: v for k, v in c.items() if k != "text"} for c in conditions]


class _BadThenGoodWriter:
    """첫 시도는 근거 없는 숫자를 쓰고, 다시 쓰라는 피드백을 받으면 고친다."""

    def __init__(self):
        self.calls: list[list[str]] = []

    async def write(self, drafts, feedback):
        self.calls.append(feedback)
        texts = await TemplateConditionWriter().write(drafts, feedback)
        return texts if feedback else [t + " (추가 비용 999원)" for t in texts]


class _AlwaysBadWriter:
    async def write(self, drafts, feedback):
        return ["추가 비용 999원" for _ in drafts]


def test_quote_matches_expected():
    source = MockQuoteDataSource()
    out = _run(source)
    quote = out["quote_draft"]

    assert out["status"] == "DRAFT"
    assert out["quote_id"] == quote["quote_id"] == "Q-2026-1019-001"
    assert out["attempts"] == 1
    # 문구(text)는 writer마다 다르므로 구조·숫자만 대조
    assert _without_text(quote["conditions"]) == _without_text(EXPECTED["conditions"])
    for key in EXPECTED.keys() - {"conditions", "quote_id"}:
        assert quote[key] == EXPECTED[key], key


def test_template_texts_use_only_supported_numbers():
    out = _run(MockQuoteDataSource())
    for condition, facts in zip(out["quote_draft"]["conditions"], out["condition_facts"]):
        assert condition["text"]
        assert find_unsupported_numbers(condition["text"], facts) == []


class _ExpectedTextWriter:
    """정답 파일 문구를 그대로 쓴다 (LLM이 잘 쓴 문구 대용)."""

    async def write(self, drafts, feedback):
        return [c["text"] for c in EXPECTED["conditions"]]


def test_expected_texts_pass_number_check():
    out = _run(MockQuoteDataSource(), _ExpectedTextWriter())
    assert out["attempts"] == 1
    assert out["quote_draft"]["conditions"] == EXPECTED["conditions"]
    assert out["quote_draft"]["checks"] == EXPECTED["checks"]


def test_save_payload_excludes_quote_id_and_status():
    source = MockQuoteDataSource()
    _run(source)
    payload = source.saved[0]
    assert payload["thread_id"] == "thread-1" and payload["review_id"] == 1
    assert "quote_id" not in payload and "status" not in payload
    assert payload["unit_price_krw"] == 37300


def test_unsupported_number_triggers_rewrite():
    writer = _BadThenGoodWriter()
    out = _run(MockQuoteDataSource(), writer)
    assert out["status"] == "DRAFT"
    assert out["attempts"] == 2
    assert any("PRICE_RECALC" in f for f in writer.calls[1])


def test_rewrite_limit_fails_graph():
    source = MockQuoteDataSource()
    out = _run(source, _AlwaysBadWriter())
    assert out["status"] == "FAILED"
    assert out["attempts"] == MAX_ATTEMPTS
    assert source.saved == []


def test_margin_failure_saves_without_rewrite():
    # 모터 원가를 올려 6년차 마진이 30% 아래로 → 재작성 없이 FAIL 표시로 저장, SG4 승인에서 판단
    source = MockQuoteDataSource(part_cost_override={"P-MTR-BL12": 12000})
    out = _run(source)
    margin = next(c for c in out["quote_draft"]["checks"] if c["rule"] == "MARGIN")
    assert margin["result"] == "FAIL"
    assert out["attempts"] == 1
    assert out["status"] == "DRAFT" and len(source.saved) == 1


def test_quote_without_conditions_is_saved():
    # 가정값·대체 부품·계약 리스크가 모두 없는 RFQ도 FORMAT을 통과해 한 번에 저장된다
    base = _input()
    out = _run(
        MockQuoteDataSource(),
        rfq_review={**base["rfq_review"], "assumptions": [], "contract_risks": []},
        supply_analysis={**base["supply_analysis"], "recommended_alt": None},
    )
    assert out["quote_draft"]["conditions"] == []
    assert all(c["result"] == "PASS" for c in out["quote_draft"]["checks"])
    assert out["status"] == "DRAFT" and out["attempts"] == 1


def test_save_conflict_marks_failed():
    out = _run(MockQuoteDataSource(save_error="409 이미 승인된 견적"))
    assert out["status"] == "FAILED"
    assert "409" in out["error"]


def test_reject_reason_reaches_writer():
    writer = _BadThenGoodWriter()
    _run(MockQuoteDataSource(), writer, reject_reason="보증 조건 문구 수정")
    assert any("보증 조건 문구 수정" in f for f in writer.calls[0])
