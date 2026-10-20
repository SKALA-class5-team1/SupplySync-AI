"""SG4 동작 확인: 승인 대기 -> resume -> 재확인 -> 등록.

MCP 호출(_call_tool)만 가짜로 바꾸고, 응답은 MCP 도구 명세의 예시 값을 쓴다.
실행: 프로젝트 루트에서  python tests/test_approval_registration_graph.py
"""
import asyncio
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.approval_registration import tools  # noqa: E402
from app.approval_registration.graph import build_graph  # noqa: E402

BASE_INPUT = {
    "thread_id": "task-demo-1",
    "rfq_id": 1,
    "quote_id": "Q-2026-1019-001",
    "model_id": "EWP-12-B",
    "options": ["OPT-HT", "OPT-IP9K"],
    "qty": 2000,
    "due_date": "2026-12-15",
    "expected_shortages": [{"part_id": "P-MCU-G1", "shortage": 500}],
    "quote_summary": "EWP-12-B + OPT-HT + OPT-IP9K, 단가 37,300원",
    "approval_items": ["고객 요구 보증 5년 15만km", "시작품 500개 대체 칩 적용"],
}

# MCP 도구 명세의 예시 응답
ATP_SAMPLE = {
    "has_shortage": True,
    "parts": [
        {"part_id": "P-MCU-G1", "required": 2000, "available": 1500, "shortage": 500,
         "incoming_qty": 5000, "incoming_date": "2027-01-20", "lead_time_days": 120},
        {"part_id": "P-MTR-BL12", "required": 2000, "available": 12000, "shortage": 0},
        {"part_id": "P-SEAL-9K", "required": 2000, "available": 7000, "shortage": 0},
    ],
}
RAG_SAMPLE = [
    {"chunk_id": "pol-003", "section": "제12조 품질 보증 기간",
     "content": "품질 보증 기간은 양산 시작일로부터 3년 또는 ...", "score": 0.82},
]


class FakeMcp:
    """_call_tool 대역. 호출 기록을 남긴다."""

    def __init__(self, atp):
        self.atp = atp
        self.calls = []

    async def __call__(self, name, arguments, *, thread_id=None, idempotency_key=None):
        self.calls.append((name, arguments, thread_id, idempotency_key))
        if name == "business_get_atp":
            return self.atp
        if name == "rag_search_docs":
            return RAG_SAMPLE
        if name == "business_register_quote":
            return {"quote_id": arguments["quote_id"], "status": "REGISTERED",
                    "registered_at": "2026-10-19T15:30:00"}
        raise AssertionError(f"예상하지 못한 도구 호출: {name}")

    def names(self):
        return [c[0] for c in self.calls]


async def _start(atp):
    fake = FakeMcp(atp)
    tools._call_tool = fake
    app = build_graph().compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "task-demo-1"}}
    first = await app.ainvoke(BASE_INPUT, cfg)
    return app, cfg, first, fake


def test_approval_then_register():
    async def run():
        app, cfg, first, fake = await _start(ATP_SAMPLE)
        assert "__interrupt__" in first, "승인 요청에서 멈춰야 한다"
        payload = first["__interrupt__"][0].value
        assert payload["event"] == "APPROVAL_REQUIRED"
        assert payload["card"]["evidence"][0]["chunk_id"] == "pol-003", "규정 근거가 카드에 있어야 한다"
        assert "business_register_quote" not in fake.names(), "승인 전에는 등록하면 안 된다"

        done = await app.ainvoke(Command(resume={"approved": True, "approved_by": 2}), cfg)
        assert done["atp_ok"] is True, "부족분이 승인 때와 같으면 통과해야 한다"
        assert done["registration_status"] == "REGISTERED"
        assert done["registered_quote_id"] == "Q-2026-1019-001"

        reg = [c for c in fake.calls if c[0] == "business_register_quote"][0]
        assert reg[1] == {"quote_id": "Q-2026-1019-001", "approved_by": 2}
        assert reg[2] == "task-demo-1"
        assert reg[3] == "task-demo-1:register_quote:Q-2026-1019-001", "멱등 키가 전달돼야 한다"
    asyncio.run(run())


def test_no_register_when_stock_worse():
    async def run():
        worse = copy.deepcopy(ATP_SAMPLE)
        worse["parts"][0].update(available=1200, shortage=800)  # 승인 때(500)보다 나빠짐
        app, cfg, _, fake = await _start(worse)
        done = await app.ainvoke(Command(resume={"approved": True, "approved_by": 2}), cfg)
        assert done["atp_ok"] is False
        assert "business_register_quote" not in fake.names(), "재고가 나빠지면 등록하면 안 된다"
        assert "P-MCU-G1" in done["error"]
    asyncio.run(run())


def test_no_register_without_approver():
    async def run():
        app, cfg, _, fake = await _start(ATP_SAMPLE)
        done = await app.ainvoke(Command(resume={"approved": True}), cfg)
        assert "business_register_quote" not in fake.names(), "승인자 없이 등록하면 안 된다"
        assert "approved_by" in done["error"]
    asyncio.run(run())


if __name__ == "__main__":
    test_approval_then_register()
    print("OK: 승인 대기 -> resume -> 재확인 -> 등록 (멱등 키, 승인자 전달 확인)")
    test_no_register_when_stock_worse()
    print("OK: 재고가 승인 때보다 나빠지면 등록하지 않음")
    test_no_register_without_approver()
    print("OK: 승인자 정보가 없으면 등록하지 않음")
