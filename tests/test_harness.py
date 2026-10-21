"""Harness 기준 확인: 개인정보 마스킹, 실패 시 FAILED, MCP 호출 시간 제한, 추적 설정.
실행: 프로젝트 루트에서  python -m tests.test_harness
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.approval_registration import tools  # noqa: E402
from app.approval_registration.graph import build_graph  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.guardrails import mask_pii, truncate_text  # noqa: E402
from app.core.observability import run_config  # noqa: E402
from tests.test_approval_registration_graph import BASE_INPUT, approve, REGISTER_SAMPLE, SUPPLY_SAMPLE  # noqa: E402

REAL_CALL_TOOL = tools._call_tool  # 다른 테스트가 가짜로 바꾸기 전의 진짜 함수


def test_mask():
    out = mask_pii("담당 김철수 010-1234-5678, kim@corp.co.kr 로 연락")
    assert "010" not in out and "@" not in out, out
    assert mask_pii("재고 1500개, 단가 37,300원") == "재고 1500개, 단가 37,300원"


def test_run_config_trace_id():
    cfg = run_config("task-1")
    assert cfg["configurable"]["thread_id"] == "task-1" and cfg["metadata"]["trace_id"] == "task-1"


async def _run(fake, inp):
    tools._call_tool = fake
    app = build_graph().compile(checkpointer=InMemorySaver())
    cfg = run_config("t")
    await app.ainvoke(inp, cfg)
    return await app.ainvoke(Command(resume=approve()), cfg)


def test_card_masks_pii():
    async def fake(server, name, arguments):
        if name == "rag_search_docs":
            return []
        if name == "check_parts_supply":
            return SUPPLY_SAMPLE
        return REGISTER_SAMPLE
    inp = {**BASE_INPUT, "quote_summary": "문의 010-1111-2222", "approval_items": ["a@b.com 승인"]}
    tools._call_tool = fake
    app = build_graph().compile(checkpointer=InMemorySaver())
    res = asyncio.run(app.ainvoke(inp, run_config("t")))
    card = res["approval_card"]
    assert "010" not in card.summary and "@" not in card.approval_items[0]


def test_recheck_failure_is_failed():
    async def fake(server, name, arguments):
        if name == "rag_search_docs":
            return []
        raise RuntimeError("연결 실패 010-9999-8888")
    res = asyncio.run(_run(fake, BASE_INPUT))
    assert res["sg4_outcome"] == "FAILED" and "010" not in res["error"], res


def test_register_failure_is_failed():
    async def fake(server, name, arguments):
        if name == "rag_search_docs":
            return []
        if name == "check_parts_supply":
            return SUPPLY_SAMPLE
        raise RuntimeError("등록 중 끊김")
    res = asyncio.run(_run(fake, BASE_INPUT))
    assert res["sg4_outcome"] == "FAILED" and res.get("registration_status") is None, res


def test_timeout_retries_then_raises():
    settings.MCP_CALL_TIMEOUT_SEC = 0.05
    calls = []

    async def slow(*a, **k):
        calls.append(1)
        await asyncio.sleep(1)

    orig = tools._call_once
    tools._call_once = slow
    try:
        try:
            asyncio.run(REAL_CALL_TOOL("business", "check_parts_supply", {}))
            raise AssertionError("시간 초과여야 한다")
        except asyncio.TimeoutError:
            pass
    finally:
        tools._call_once = orig
        settings.MCP_CALL_TIMEOUT_SEC = 120.0
    assert len(calls) == 3, calls  # 처음 1회 + 재시도 2회


def test_truncate():
    assert truncate_text("가" * 10, 100) == "가" * 10
    out = truncate_text("가" * 50, 10)
    assert out.startswith("가" * 10) and "생략" in out and len(out) < 50


def test_rag_result_is_truncated():
    async def fake(server, name, arguments):
        return [{"chunk_id": "c", "section": "s", "content": "가" * 5000, "score": 0.5}]
    tools._call_tool = fake
    hits = asyncio.run(tools.rag_search_docs("q"))
    assert len(hits[0].content) < 2100, len(hits[0].content)


def test_total_time_budget():
    settings.TASK_TIMEOUT_SEC = 0.1
    settings.MCP_CALL_TIMEOUT_SEC = 5
    calls = []

    async def slow(*a, **k):
        calls.append(1)
        await asyncio.sleep(1)

    orig = tools._call_once
    tools._call_once = slow
    try:
        try:
            asyncio.run(REAL_CALL_TOOL("business", "check_parts_supply", {}))
            raise AssertionError("시간 초과여야 한다")
        except asyncio.TimeoutError:
            pass
    finally:
        tools._call_once = orig
        settings.TASK_TIMEOUT_SEC = 120.0
        settings.MCP_CALL_TIMEOUT_SEC = 120.0
    assert len(calls) == 1, calls  # 전체 시간이 다 써서 재시도하지 않는다


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("OK ", name)
