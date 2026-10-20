"""SG4 동작 확인: 승인 대기 -> resume -> 재확인 -> 등록.

MCP 호출(_call_tool)만 가짜로 바꾼다. 도구 이름·응답 모양은 business-mcp 의 CLAUDE.md 명세와
정답 파일(7_sg4_register.json)을 따른다.
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
    "final_plan": {"P-MCU-G1": 1500, "P-MCU-G1-ALT": 500},
    "due_date": "2026-12-15",
    "base_date": "2026-10-19",
    "quote_summary": "EWP-12-B + OPT-HT + OPT-IP9K, 단가 37,300원",
    "approval_items": ["보증 5년 수용 시 품질본부장 승인 필요", "시작품 500개 대체 칩 적용"],
}

# check_parts_supply 응답 (7_sg4_register.json: 재고 변동 없음)
SUPPLY_SAMPLE = {
    "due_date": "2026-12-15",
    "base_date": "2026-10-19",
    "has_risk": False,
    "parts": [
        {"part_id": "P-MCU-G1", "part_name": "제어칩 (고온용)", "required_qty": 1500,
         "available": 1500, "short_qty": 0, "status": "OK", "risks": []},
        {"part_id": "P-MCU-G1-ALT", "part_name": "제어칩 (고온용, 대체)", "required_qty": 500,
         "available": 3000, "short_qty": 0, "status": "OK", "risks": []},
    ],
}
RAG_SAMPLE = [
    {"chunk_id": "pol-003", "section": "제12조 품질 보증 기간",
     "content": "품질 보증 기간은 양산 시작일로부터 3년 또는 ...", "score": 0.82},
]
REGISTER_SAMPLE = {"quote_id": "Q-2026-1019-001", "status": "REGISTERED",
                   "rfq_status": "QUOTED", "registered_at": "2026-10-19T15:30:00"}


class FakeMcp:
    """_call_tool 대역. 호출 기록을 남긴다."""

    def __init__(self, supply):
        self.supply = supply
        self.calls = []

    async def __call__(self, server, name, arguments):
        self.calls.append((server, name, arguments))
        if name == "check_parts_supply":
            return self.supply
        if name == "search_docs":
            return RAG_SAMPLE
        if name == "register_quote":
            return REGISTER_SAMPLE
        raise AssertionError(f"예상하지 못한 도구 호출: {name}")

    def names(self):
        return [c[1] for c in self.calls]


async def _start(supply):
    fake = FakeMcp(supply)
    tools._call_tool = fake
    app = build_graph().compile(checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "task-demo-1"}}
    first = await app.ainvoke(BASE_INPUT, cfg)
    return app, cfg, first, fake


def test_approval_then_register():
    async def run():
        app, cfg, first, fake = await _start(SUPPLY_SAMPLE)
        assert "__interrupt__" in first, "승인 요청에서 멈춰야 한다"
        payload = first["__interrupt__"][0].value
        assert payload["event"] == "APPROVAL_REQUIRED"
        assert payload["card"]["evidence"][0]["chunk_id"] == "pol-003", "규정 근거가 카드에 있어야 한다"
        assert "register_quote" not in fake.names(), "승인 전에는 등록하면 안 된다"

        done = await app.ainvoke(Command(resume={"approved": True}), cfg)
        assert done["recheck_ok"] is True, "계획 수량이 가용 재고 안이면 통과해야 한다"
        assert done["registration_status"] == "REGISTERED"
        assert done["registered_quote_id"] == "Q-2026-1019-001"

        check = [c for c in fake.calls if c[1] == "check_parts_supply"][0]
        assert check[0] == "business"
        assert check[2]["due_date"] == "2026-12-15"
        assert check[2]["parts"] == [
            {"part_id": "P-MCU-G1", "required_qty": 1500},
            {"part_id": "P-MCU-G1-ALT", "required_qty": 500},
        ], "대체 부품도 일반 부품처럼 최종 계획 수량으로 확인해야 한다"
        reg = [c for c in fake.calls if c[1] == "register_quote"][0]
        assert reg[2] == {"quote_id": "Q-2026-1019-001", "thread_id": "task-demo-1"}
    asyncio.run(run())


def test_no_register_when_stock_short():
    async def run():
        short = copy.deepcopy(SUPPLY_SAMPLE)
        short["parts"][0].update(available=1200, short_qty=300, status="SHORT")
        app, cfg, _, fake = await _start(short)
        done = await app.ainvoke(Command(resume={"approved": True}), cfg)
        assert done["recheck_ok"] is False
        assert "register_quote" not in fake.names(), "재고가 부족해지면 등록하면 안 된다"
        assert "P-MCU-G1" in done["error"]
    asyncio.run(run())


def test_no_register_when_part_missing():
    async def run():
        missing = copy.deepcopy(SUPPLY_SAMPLE)
        missing["parts"] = missing["parts"][:1]  # 대체 칩 응답이 빠짐
        app, cfg, _, fake = await _start(missing)
        done = await app.ainvoke(Command(resume={"approved": True}), cfg)
        assert done["recheck_ok"] is False
        assert "register_quote" not in fake.names(), "확인하지 못한 부품이 있으면 등록하면 안 된다"
        assert "P-MCU-G1-ALT" in done["error"]
    asyncio.run(run())


if __name__ == "__main__":
    test_approval_then_register()
    print("OK: 승인 대기 -> resume -> 재확인 -> 등록 (최종 계획 수량으로 확인, 등록 인자 확인)")
    test_no_register_when_stock_short()
    print("OK: 재고가 부족해지면 등록하지 않음")
    test_no_register_when_part_missing()
    print("OK: 응답에 빠진 부품이 있으면 등록하지 않음")
