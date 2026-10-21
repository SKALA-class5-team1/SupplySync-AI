"""SG3 서브그래프 Harness 테스트

실제 모델·MCP는 부르지 않는다.
- MCP: tools._call_once만 가짜로 바꿔 재시도·시간 제한(_call_tool)은 실제 코드로 돈다. 값은 dataset v2.2 시드 기준.
- 모델: bind_tools/ainvoke만 흉내 내는 FakeModel. 정해 둔 응답(AIMessage)이나 예외를 차례로 돌려준다.
기대값은 fixtures/6_sg3_quote.json(05_expected 복사본)과 대조한다.
"""
import asyncio
import json
from pathlib import Path

import pytest
import yaml
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.core.observability import run_config
from app.quote_drafting import model as model_module
from app.quote_drafting import nodes, tools
from app.quote_drafting.context import compact_messages
from app.quote_drafting.graph import build_graph
from app.quote_drafting.harness import MAX_SAME_TOOL_CALLS, MAX_TURNS, RUN_NAME, load_prompt

FIXTURES = Path(__file__).resolve().parent / "fixtures"
RULES = yaml.safe_load((FIXTURES / "rules_sg3.yaml").read_text(encoding="utf-8"))
THREAD_ID = "task-sg3-1"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


EXPECTED = _load("6_sg3_quote")["quote_draft"]
GOOD_TEXTS = [c["text"] for c in EXPECTED["conditions"]]


def _sg1_state() -> dict:
    """SG1이 state에 넣는 키 (SG1State 출력). 값은 05_expected/4_output_state.json."""
    review = _load("4_output_state")
    return {
        "rfq_number": review["rfq_id"],
        "review_id": 1,
        "parsed_rfq": _load("1_parsed_rfq"),
        "selected_model": review["selected_model"],
        "options": review["options"],
        "assumptions": review["assumptions"],
        "contract_risks": review["contract_risks"],
        "demand": {**review["demand"], "target_price_krw": review["target_price_krw"]},
    }


def _input(**overrides) -> dict:
    return {
        "thread_id": THREAD_ID,
        **_sg1_state(),
        "supply_analysis": _load("5_sg2_supply"),
        "issue_date": "2026-10-19",
        **overrides,
    }


# --- 가짜 MCP (v2.2 시드) ---

_PART_COST = {
    "P-MTR-BL12": 9500, "P-MCU-STD": 3200, "P-MCU-G1": 5300, "P-IMP-25": 1200,
    "P-HSG-AL": 4100, "P-SEAL-STD": 400, "P-SEAL-9K": 950, "P-CON-4P": 600,
}
_BASE_BOM = ["P-MTR-BL12", "P-MCU-STD", "P-IMP-25", "P-HSG-AL", "P-SEAL-STD", "P-CON-4P"]
_OPTION_REPLACES = {"OPT-HT": ("P-MCU-STD", "P-MCU-G1"), "OPT-IP9K": ("P-SEAL-STD", "P-SEAL-9K")}
_CHUNKS = {
    "제12조": {"chunk_id": "pol-003", "section": "제12조 품질 보증 기간",
              "content": "품질 보증 기간은 양산 시작일로부터 3년 또는 주행거리 100,000 km 중 먼저 도래하는 기간을 넘을 수 없다.",
              "score": 0.82},
}


class StatusError(Exception):
    """HTTP 상태 코드가 있는 오류 (openai.RateLimitError, httpx 오류 흉내)."""

    def __init__(self, status_code: int, message: str = ""):
        super().__init__(message or f"HTTP {status_code}")
        self.status_code = status_code


class FakeMcp:
    """tools._call_once 대역. failures[name]에 넣은 예외를 순서대로 먼저 던진다."""

    def __init__(self, part_cost=None, chunks=None, failures=None):
        self.part_cost = {**_PART_COST, **(part_cost or {})}
        self.chunks = chunks if chunks is not None else _CHUNKS
        self.failures = {k: list(v) for k, v in (failures or {}).items()}
        self.calls = []

    async def __call__(self, server, name, arguments):
        self.calls.append((server, name, arguments))
        if self.failures.get(name):
            raise self.failures[name].pop(0)
        if name == "get_options":
            return {"model_id": arguments["model_id"], "base_price_krw": 34000,
                    "options": [{"option_code": "OPT-HT", "add_price_krw": 2400},
                                {"option_code": "OPT-IP9K", "add_price_krw": 900}]}
        if name == "get_parts_cost":
            parts = list(_BASE_BOM)
            for code in arguments["options"]:
                old, new = _OPTION_REPLACES[code]
                parts[parts.index(old)] = new
            rows = [{"part_id": p, "qty_per_unit": 1, "unit_cost_krw": self.part_cost[p],
                     "cost_krw": self.part_cost[p]} for p in parts]
            return {"model_id": arguments["model_id"], "options": arguments["options"],
                    "parts_cost_krw": sum(r["cost_krw"] for r in rows), "parts_cost_breakdown": rows}
        if name == "search_docs":
            hit = self.chunks.get(arguments["query"])
            return [hit] if hit else []
        if name == "save_quote":
            return {"quote_id": "Q-2026-1019-001", "status": "DRAFT"}
        raise AssertionError(f"예상하지 못한 도구 호출: {name}")

    def names(self):
        return [c[1] for c in self.calls]


# --- 가짜 모델 ---


class FakeModel:
    """bind_tools/ainvoke만 흉내 낸다. script의 AIMessage를 돌려주거나 예외를 던진다. 다 쓰면 마지막 것을 반복."""

    def __init__(self, script, delay: float = 0.0):
        self.script = list(script)
        self.delay = delay
        self.received = []

    def bind_tools(self, _tools):
        return self

    async def ainvoke(self, messages):
        self.received.append(list(messages))
        if self.delay:
            await asyncio.sleep(self.delay)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, Exception):
            raise item
        return item


_ids = iter(range(1, 10_000))


def search(query: str) -> AIMessage:
    return AIMessage("", tool_calls=[{"name": "search_policy_docs", "args": {"query": query}, "id": f"call_{next(_ids)}"}])


def submit(texts) -> AIMessage:
    args = {"conditions": [{"index": i, "text": t} for i, t in enumerate(texts)]}
    return AIMessage("", tool_calls=[{"name": "submit_conditions", "args": args, "id": f"call_{next(_ids)}"}])


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(model_module, "RETRY_BACKOFF_SEC", 0)
    monkeypatch.setattr(tools, "RETRY_BACKOFF_SEC", 0)


def decision(result: str, reason=None, changes=None, step: str = "SG3.request_save_approval") -> dict:
    """저장 승인 resume 본문 (SG4 resume과 같은 형식, step만 다름)."""
    return {"step": step, "userId": 2, "decision": {"result": result, "reason": reason, "changes": changes or {}}}


APPROVE = decision("APPROVED")


def _setup(monkeypatch, mcp=None, script=None, fallback_script=None, delay=0.0):
    mcp = mcp or FakeMcp()
    monkeypatch.setattr(tools, "_call_once", mcp)
    llm = FakeModel(script or [submit(GOOD_TEXTS)], delay=delay)
    fallback = FakeModel(fallback_script or [AIMessage("대체 모델 응답 없음")])
    app = build_graph(llm, fallback, rules=RULES).compile(checkpointer=InMemorySaver())
    return app, mcp, llm, fallback


def _run(app, value):
    return asyncio.run(app.ainvoke(value, run_config(THREAD_ID, RUN_NAME)))


def _interrupt(out) -> dict:
    assert "__interrupt__" in out, f"저장 승인에서 멈춰야 한다: {out.get('sg3_outcome')} {out.get('error')}"
    return out["__interrupt__"][0].value


# --- 정상 흐름 + interrupt 재개 ---


def test_normal_flow_waits_for_approval_then_saves(monkeypatch):
    app, mcp, llm, _ = _setup(monkeypatch, script=[search("제12조"), submit(GOOD_TEXTS)])
    first = _run(app, _input())

    payload = _interrupt(first)
    assert payload["event"] == "SAVE_APPROVAL_REQUIRED"
    assert payload["card"]["quote_summary"] == "EWP-12-B + OPT-HT + OPT-IP9K, 단가 37,300원"
    assert "save_quote" not in mcp.names(), "승인 전에는 저장하면 안 된다"

    done = _run(app, Command(resume=APPROVE))  # 같은 thread_id로 재개
    assert done["sg3_outcome"] == "DRAFT_SAVED"
    assert done["quote_id"] == "Q-2026-1019-001"
    assert done["evidence_ids"] == ["pol-003"]
    assert any("대체 제어칩" in item for item in done["approval_items"])

    quote = done["quote_draft"]
    # 수신처는 SG1 parse_rfq의 customer를 그대로 쓴다 (정답 파일의 "한별모터스 구매본부"와 다름)
    assert quote["recipient"] == "한별모터스"
    for key in EXPECTED.keys() - {"quote_id", "recipient"}:
        assert quote[key] == EXPECTED[key], key

    payload = [c for c in mcp.calls if c[1] == "save_quote"][0][2]
    assert payload["thread_id"] == THREAD_ID and payload["revision"] == 1
    assert "review_id" not in payload, "백엔드는 thread_id로 검토 결과를 찾는다"
    assert "quote_id" not in payload and "status" not in payload
    assert len(llm.received) == 2


def test_revise_from_sg4_saves_next_revision(monkeypatch):
    # SG4 수정 요청: 상위 그래프가 draft_revision을 +1 해서 직전 quote_id·사유·수정 내용과 함께 넘긴다
    app, mcp, llm, _ = _setup(monkeypatch, script=[submit(GOOD_TEXTS)])
    _interrupt(_run(app, _input(
        draft_revision=2, quote_id="Q-2026-1019-001", reject_reason="보증 문구 수정",
        changes={"warranty": "3년 / 100,000 km 유지"},
    )))
    task = json.loads(llm.received[0][1].content)
    assert any("보증 문구 수정" in f for f in task["feedback"])
    assert any("3년 / 100,000 km 유지" in f for f in task["feedback"]), "changes도 재작성에 반영"

    done = _run(app, Command(resume=APPROVE))
    assert done["sg3_outcome"] == "DRAFT_SAVED"
    assert done["draft_revision"] == 2, "받은 차수 그대로 돌려준다 (+1은 상위 그래프가 함)"
    payload = [c for c in mcp.calls if c[1] == "save_quote"][0][2]
    assert payload["revision"] == 2


def test_sg4_contract_fields(monkeypatch):
    # SG3 -> SG4: quote_id, quote_summary, approval_items, evidence_ids, draft_revision (SG4 연결 약속)
    app, _, _, _ = _setup(monkeypatch, script=[search("제12조"), submit(GOOD_TEXTS)])
    _run(app, _input())
    done = _run(app, Command(resume=APPROVE))
    assert done["quote_id"] == "Q-2026-1019-001"
    assert done["quote_summary"] == "EWP-12-B + OPT-HT + OPT-IP9K, 단가 37,300원"
    assert done["approval_items"] and done["evidence_ids"] == ["pol-003"]
    assert done["draft_revision"] == 1
    assert "approval_card" not in done, "SG4의 approval_card 칸을 쓰지 않는다"


def test_revision_over_max_is_rejected(monkeypatch):
    app, _, _, _ = _setup(monkeypatch)
    with pytest.raises(Exception):
        _run(app, _input(draft_revision=5))


def test_rewrite_request_on_approval_then_resume(monkeypatch):
    app, mcp, llm, _ = _setup(monkeypatch, script=[submit(GOOD_TEXTS), submit(GOOD_TEXTS)])
    _interrupt(_run(app, _input()))

    again = _run(app, Command(resume=decision("REVISE", "보증 문구 수정. 담당 010-1234-5678", {"warranty": "3년 유지"})))
    _interrupt(again)  # 다시 써서 다시 멈춘다
    feedback = llm.received[-1][-1]
    assert isinstance(feedback, HumanMessage) and "보증 문구 수정" in feedback.content
    assert "010-1234-5678" not in feedback.content and "[전화번호]" in feedback.content
    assert "3년 유지" in feedback.content, "decision.changes도 재작성에 반영"

    done = _run(app, Command(resume=APPROVE))
    assert done["sg3_outcome"] == "DRAFT_SAVED"
    assert mcp.names().count("save_quote") == 1


def test_save_rejected_by_approver_ends_without_saving(monkeypatch):
    app, mcp, _, _ = _setup(monkeypatch)
    _interrupt(_run(app, _input()))
    out = _run(app, Command(resume=decision("REJECTED", "단가 재검토 필요")))
    assert out["sg3_outcome"] == "REJECTED" and "단가 재검토 필요" in out["error"]
    assert "save_quote" not in mcp.names()


def test_sg4_step_resume_is_not_accepted(monkeypatch):
    # SG4 단계용 resume이 SG3 대기에 잘못 오면 승인으로 보지 않고 다시 묻는다
    app, mcp, _, _ = _setup(monkeypatch)
    payload = _interrupt(_run(app, _input()))
    assert payload["step"] == "SG3.request_save_approval"
    again = _interrupt(_run(app, Command(resume=decision("APPROVED", step="SG4.request_approval"))))
    assert "step" in again["invalid_input"]
    assert "save_quote" not in mcp.names()


def test_invalid_approval_input_asks_again(monkeypatch):
    app, mcp, _, _ = _setup(monkeypatch)
    _interrupt(_run(app, _input()))
    again = _interrupt(_run(app, Command(resume={"approved": "yes"})))
    assert "invalid_input" in again
    assert "save_quote" not in mcp.names()
    done = _run(app, Command(resume=APPROVE))
    assert done["sg3_outcome"] == "DRAFT_SAVED"


def test_no_conditions_skips_model(monkeypatch):
    app, _, llm, _ = _setup(monkeypatch)
    base = _input()
    first = _run(app, _input(
        assumptions=[],
        contract_risks=[],
        supply_analysis={**base["supply_analysis"], "recommended_alt": None},
    ))
    _interrupt(first)
    assert llm.received == []
    assert _run(app, Command(resume=APPROVE))["sg3_outcome"] == "DRAFT_SAVED"


# --- 도구 오류 → ToolMessage ---


def test_tool_error_returned_as_tool_message(monkeypatch):
    mcp = FakeMcp(failures={"search_docs": [tools.ToolError("rag-mcp 검색 실패")]})
    app, _, llm, _ = _setup(monkeypatch, mcp=mcp, script=[search("제12조"), submit(GOOD_TEXTS)])
    _interrupt(_run(app, _input()))

    tool_msg = llm.received[1][-1]
    assert isinstance(tool_msg, ToolMessage) and tool_msg.status == "error"
    assert "도구 오류" in tool_msg.content and "rag-mcp 검색 실패" in tool_msg.content


def test_validation_failure_returned_as_tool_message(monkeypatch):
    bad = [t + " 추가 비용 999원" for t in GOOD_TEXTS]
    app, _, llm, _ = _setup(monkeypatch, script=[submit(bad), submit(GOOD_TEXTS)])
    _interrupt(_run(app, _input()))
    tool_msg = llm.received[1][-1]
    assert isinstance(tool_msg, ToolMessage) and "검증 실패" in tool_msg.content and "PRICE_RECALC" in tool_msg.content


def test_policy_failure_goes_to_approval_without_rewrite(monkeypatch):
    # 모터 원가를 올려 6년차 마진이 30% 아래 → 문구로 못 고치므로 재작성 없이 승인 단계에서 판단
    app, _, llm, _ = _setup(monkeypatch, mcp=FakeMcp(part_cost={"P-MTR-BL12": 12000}))
    payload = _interrupt(_run(app, _input()))
    assert any(item.startswith("마진 기준 미달") for item in payload["card"]["approval_items"])
    assert len(llm.received) == 1


# --- 한도 초과 → FAILED ---


def test_turn_limit_fails(monkeypatch):
    app, mcp, llm, _ = _setup(monkeypatch, script=[AIMessage("도구 없이 답합니다")])
    out = _run(app, _input())
    assert out["sg3_outcome"] == "FAILED" and f"{MAX_TURNS}턴" in out["error"]
    assert len(llm.received) == MAX_TURNS
    assert "save_quote" not in mcp.names()


def test_same_tool_limit_fails(monkeypatch):
    app, mcp, _, _ = _setup(monkeypatch, script=[search(f"검색{i}") for i in range(MAX_SAME_TOOL_CALLS + 1)])
    out = _run(app, _input())
    assert out["sg3_outcome"] == "FAILED" and "같은 도구(search_policy_docs)" in out["error"]
    assert mcp.names().count("search_docs") == MAX_SAME_TOOL_CALLS


def test_task_timeout_fails(monkeypatch):
    monkeypatch.setattr(nodes, "TASK_TIMEOUT_SEC", 0.05)
    app, _, _, fallback = _setup(monkeypatch, delay=0.2)
    out = _run(app, _input())
    assert out["sg3_outcome"] == "FAILED" and "TaskTimeoutError" in out["error"]
    assert fallback.received == [], "작업 시간 초과는 대체 모델로 넘기지 않는다"


# --- Recovery ---


def test_model_429_retry_then_success(monkeypatch):
    app, _, llm, fallback = _setup(monkeypatch, script=[StatusError(429), submit(GOOD_TEXTS)])
    _interrupt(_run(app, _input()))
    assert len(llm.received) == 2
    assert fallback.received == []


def test_model_retries_exhausted_uses_fallback(monkeypatch):
    app, _, llm, fallback = _setup(
        monkeypatch,
        script=[StatusError(503), StatusError(503), StatusError(503), submit(GOOD_TEXTS)],
        fallback_script=[submit(GOOD_TEXTS)],
    )
    _interrupt(_run(app, _input()))
    assert len(llm.received) == 3, "같은 모델로 2회 재시도 (총 3회)"
    assert len(fallback.received) == 1


def test_model_and_fallback_fail_masks_error(monkeypatch):
    app, _, _, fallback = _setup(
        monkeypatch,
        script=[StatusError(429, "rate limit for ops@daesung.com")],
        fallback_script=[StatusError(503, "unavailable, call 010-9876-5432")],
    )
    out = _run(app, _input())
    assert out["sg3_outcome"] == "FAILED"
    assert "ops@daesung.com" not in out["error"] and "010-9876-5432" not in out["error"]
    assert "[전화번호]" in out["error"]
    assert len(fallback.received) == 1


def test_mcp_429_retry_then_success(monkeypatch):
    mcp = FakeMcp(failures={"get_options": [StatusError(429)]})
    app, _, _, _ = _setup(monkeypatch, mcp=mcp)
    _interrupt(_run(app, _input()))
    assert mcp.names().count("get_options") == 2


def test_mcp_failure_after_retries_fails(monkeypatch):
    mcp = FakeMcp(failures={"get_parts_cost": [StatusError(503)] * 3})
    app, _, llm, _ = _setup(monkeypatch, mcp=mcp)
    out = _run(app, _input())
    assert out["sg3_outcome"] == "FAILED" and "단가·원가 조회 실패" in out["error"]
    assert mcp.names().count("get_parts_cost") == 3
    assert llm.received == []


def test_save_without_response_is_not_retried(monkeypatch):
    mcp = FakeMcp(failures={"save_quote": [asyncio.TimeoutError()]})
    app, _, _, _ = _setup(monkeypatch, mcp=mcp)
    _interrupt(_run(app, _input()))
    out = _run(app, Command(resume=APPROVE))
    assert out["sg3_outcome"] == "SAVE_UNCONFIRMED" and "확인 필요" in out["error"]
    assert mcp.names().count("save_quote") == 1, "쓰기는 자동 재시도하지 않는다"


def test_save_rejected_by_server_fails(monkeypatch):
    mcp = FakeMcp(failures={"save_quote": [tools.ToolError("409 이미 승인된 견적")]})
    app, _, _, _ = _setup(monkeypatch, mcp=mcp)
    _interrupt(_run(app, _input()))
    out = _run(app, Command(resume=APPROVE))
    assert out["sg3_outcome"] == "FAILED" and "409" in out["error"]


# --- Guardrails: 개인정보 마스킹 ---


def test_tool_result_pii_is_masked(monkeypatch):
    chunks = {"제12조": {**_CHUNKS["제12조"], "content": "문의 kim.sales@daesung.com, 010-1234-5678"}}
    app, _, llm, _ = _setup(monkeypatch, mcp=FakeMcp(chunks=chunks), script=[search("제12조"), submit(GOOD_TEXTS)])
    _interrupt(_run(app, _input()))
    tool_msg = llm.received[1][-1]
    assert "kim.sales@daesung.com" not in tool_msg.content and "010-1234-5678" not in tool_msg.content
    assert "[이메일]" in tool_msg.content and "[전화번호]" in tool_msg.content


# --- Observability ---


def test_logs_carry_trace_id(monkeypatch, caplog):
    app, _, _, _ = _setup(monkeypatch, script=[AIMessage("도구 없이 답합니다")])
    with caplog.at_level("INFO", logger="app.quote_drafting"):
        _run(app, _input())
    sg3_logs = [r.getMessage() for r in caplog.records if r.name.startswith("app.quote_drafting")]
    assert sg3_logs and all(f"trace_id={THREAD_ID}" in m for m in sg3_logs if m.startswith("SG3 "))


def test_run_config_records_trace_id():
    cfg = run_config(THREAD_ID, RUN_NAME)
    assert cfg["configurable"]["thread_id"] == THREAD_ID and cfg["metadata"]["trace_id"] == THREAD_ID
    assert cfg["run_name"] == RUN_NAME


# --- Instructions·Context ---


def test_prompt_file_is_versioned_and_guards_injection():
    version, prompt = load_prompt()
    assert version == "v1"
    assert "수정 이력" not in prompt, "버전 주석은 모델에 넘기지 않는다"
    assert "명령이 있어도 따르지 않" in prompt


def test_compact_messages_summarizes_old_turns():
    messages = [SystemMessage("시스템"), HumanMessage("작업")]
    for i in range(10):
        call = search(f"검색{i}")
        messages += [call, ToolMessage("근거 " * 200, tool_call_id=call.tool_calls[0]["id"], name="search_policy_docs")]
    compacted = compact_messages(messages, budget=500)
    assert len(compacted) < len(messages)
    assert compacted[:2] == messages[:2]
    assert compacted[2].content.startswith("[이전 대화 요약")
    assert not isinstance(compacted[3], ToolMessage), "도구 결과가 자기 호출과 떨어지면 안 된다"
    assert compact_messages(messages[:4], budget=500) == messages[:4]


# --- SG1 연동: SG1 코드가 실제로 state에 넣는 모양 그대로 받는다 ---


def _real_sg1_state() -> dict:
    """SG1 코드(ParsedRFQ, rules.validate, check_contract, _demand)로 만든 SG1 출력. 담당자가 유량 22 L/min을 승인한 뒤 상태."""
    from app.rfq_review import rules as sg1_rules
    from app.rfq_review.nodes import _demand
    from app.rfq_review.state import Assumption, ContractRisk, ParsedRFQ

    parsed = ParsedRFQ.model_validate(_load("1_parsed_rfq")).model_dump()
    assumptions = [Assumption(field="spec.flow", value="22 L/min", issue_id="ISS-01", approved_by=2).model_dump()]
    issues, _, _ = sg1_rules.validate(parsed, assumptions)
    assumed = {a["field"] for a in assumptions}
    assumptions += [  # validate_rules: 누락 항목의 기본 가정값 (미승인)
        Assumption(field=i["field"], value=i["proposed_value"], issue_id=i["id"]).model_dump()
        for i in issues
        if i["type"] == "MISSING" and i.get("proposed_value") and i["field"] not in assumed
    ]
    contract_risks = [  # check_contract
        ContractRisk(item=f"{sg1_rules.label(i['field'])} {i['found']}", found=i["found"], limit=i.get("limit"),
                     ref=i.get("ref"), suggestion=i.get("suggestion"), evidence=[]).model_dump()
        for i in issues if i["type"] == "CONTRACT_RISK"
    ]
    return {
        "rfq_number": "HB-EV7-EWP-001",
        "review_id": 1,
        "parsed_rfq": parsed,
        "selected_model": "EWP-12-B",
        "options": ["OPT-HT", "OPT-IP9K"],
        "assumptions": assumptions,
        "contract_risks": contract_risks,
        "demand": _demand(parsed),
    }


def test_accepts_real_sg1_state(monkeypatch):
    app, _, _, _ = _setup(monkeypatch, script=[submit(GOOD_TEXTS)])
    out = _run(app, {**_input(), **_real_sg1_state()})
    _interrupt(out)

    quote = out["quote_draft"]
    assert [(c["type"], c.get("field"), c.get("ref")) for c in quote["conditions"]] == [
        (c["type"], c.get("field"), c.get("ref")) for c in EXPECTED["conditions"]
    ]
    assert quote["checks"] == EXPECTED["checks"], "SG1의 float 값(3.0 등)도 정답과 같은 표기로 검증"
    assert out["condition_facts"][0]["rfq_value"] == "22 L/h"

    done = _run(app, Command(resume=APPROVE))
    assert done["sg3_outcome"] == "DRAFT_SAVED"
    assert done["quote_draft"]["recipient"] == "한별모터스"
    for key in EXPECTED.keys() - {"quote_id", "recipient", "conditions"}:
        assert done["quote_draft"][key] == EXPECTED[key], key


def test_no_selected_model_fails_without_calls(monkeypatch):
    app, mcp, llm, _ = _setup(monkeypatch)
    out = _run(app, _input(selected_model=None))
    assert out["sg3_outcome"] == "FAILED" and "selected_model" in out["error"]
    assert mcp.calls == [] and llm.received == []


def test_missing_sg1_data_goes_to_approval_without_rewrite(monkeypatch):
    # SG1이 값을 못 찾은 항목(연간 물량·목표가)은 문구로 못 고치므로 재작성하지 않고 승인 단계에 보여 준다
    app, _, llm, _ = _setup(monkeypatch)
    base = _input()
    parsed = {**base["parsed_rfq"], "commercial": {**base["parsed_rfq"]["commercial"], "target_price_krw": None}}
    out = _run(app, _input(parsed_rfq=parsed, demand={**base["demand"], "eau": None, "target_price_krw": None}))
    payload = _interrupt(out)
    items = payload["card"]["approval_items"]
    assert any(item.startswith("문서 형식 확인 필요") for item in items)
    assert any(item.startswith("가격 기준 미달") and "목표가 없음" in item for item in items)
    assert len(llm.received) == 1
