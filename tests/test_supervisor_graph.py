"""상위 그래프 초안 확인: SG1~3(임시) -> SG4. 반려되면 SG3 로 돌아가 다시 작성한 뒤 승인·등록.

MCP 호출(_call_tool)만 가짜로 바꾼다.
실행: 프로젝트 루트에서  python tests/test_supervisor_graph.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from app.approval_registration import tools  # noqa: E402
from app.supervisor.graph import MAX_REVISIONS, build_supervisor_graph  # noqa: E402
from tests.test_approval_registration_graph import FakeMcp, SUPPLY_SAMPLE  # noqa: E402

INPUT = {"thread_id": "task-demo-1", "rfq_id": 1, "rfq_number": "HB-EV7-EWP-001"}
CFG = {"configurable": {"thread_id": "task-demo-1"}}


def _app():
    fake = FakeMcp(SUPPLY_SAMPLE)
    tools._call_tool = fake
    return build_supervisor_graph().compile(checkpointer=InMemorySaver()), fake


def test_approve_flow():
    async def run():
        app, fake = _app()
        first = await app.ainvoke(INPUT, CFG)
        assert first["__interrupt__"][0].value["event"] == "APPROVAL_REQUIRED"
        assert "register_quote" not in fake.names()
        done = await app.ainvoke(Command(resume={"approved": True}), CFG)
        assert done["registration_status"] == "REGISTERED"
        assert done["sg4_outcome"] == "REGISTERED"
        assert done["draft_revision"] == 1, "SG3 는 한 번만 돌아야 한다"
    asyncio.run(run())


def test_reject_goes_back_to_sg3_then_approve():
    async def run():
        app, fake = _app()
        await app.ainvoke(INPUT, CFG)
        second = await app.ainvoke(
            Command(resume={"approved": False, "reject_reason": "보증 조건 재협의"}), CFG
        )
        # 반려 -> SG3 가 다시 작성(2번째) -> SG4 가 다시 승인 대기
        assert "__interrupt__" in second, "재작성 뒤 다시 승인을 기다려야 한다"
        card = second["__interrupt__"][0].value["card"]
        assert "보증 조건 재협의" in card["summary"], "반려 사유가 재작성에 반영돼야 한다"
        assert "register_quote" not in fake.names(), "반려 중에는 등록하면 안 된다"

        done = await app.ainvoke(Command(resume={"approved": True}), CFG)
        assert done["draft_revision"] == 2
        assert done["registration_status"] == "REGISTERED"
        assert done["rejected"] is False, "다시 승인되면 반려 표시가 풀려야 한다"
    asyncio.run(run())


def test_reject_limit():
    async def run():
        app, fake = _app()
        out = await app.ainvoke(INPUT, CFG)
        for _ in range(MAX_REVISIONS):
            out = await app.ainvoke(Command(resume={"approved": False, "reject_reason": "또 반려"}), CFG)
        assert "__interrupt__" not in out, "반려 횟수 한도를 넘으면 멈춰야 한다"
        assert out["rejected"] is True
        assert "register_quote" not in fake.names()
    asyncio.run(run())


def test_stock_shortage_does_not_loop_to_sg3():
    async def run():
        import copy
        short = copy.deepcopy(SUPPLY_SAMPLE)
        short["parts"][0].update(available=1200, short_qty=300, status="SHORT")
        fake = FakeMcp(short)
        tools._call_tool = fake
        app = build_supervisor_graph().compile(checkpointer=InMemorySaver())
        await app.ainvoke(INPUT, CFG)
        out = await app.ainvoke(Command(resume={"approved": True}), CFG)
        assert out["sg4_outcome"] == "STOCK_REVIEW_REQUIRED"
        assert out["draft_revision"] == 1, "재고 부족은 반려가 아니므로 SG3 로 돌아가면 안 된다"
        assert "__interrupt__" not in out
        assert "register_quote" not in fake.names()
    asyncio.run(run())


def test_invalid_resume_keeps_waiting_in_sg4():
    async def run():
        app, fake = _app()
        await app.ainvoke(INPUT, CFG)
        out = await app.ainvoke(Command(resume={"approved": "maybe"}), CFG)
        assert "__interrupt__" in out, "잘못된 입력이면 SG4 에서 계속 기다려야 한다"
        assert out["__interrupt__"][0].value.get("invalid_input")
        assert "register_quote" not in fake.names()
    asyncio.run(run())


if __name__ == "__main__":
    test_approve_flow()
    print("OK: SG1~3 -> SG4 승인 -> 등록")
    test_reject_goes_back_to_sg3_then_approve()
    print("OK: 반려 -> SG3 재작성 -> 다시 승인 -> 등록")
    test_reject_limit()
    print(f"OK: 반려가 {MAX_REVISIONS}회 쌓이면 종료")
    test_stock_shortage_does_not_loop_to_sg3()
    print("OK: 재고 부족은 SG3 로 돌아가지 않고 등록도 하지 않음")
    test_invalid_resume_keeps_waiting_in_sg4()
    print("OK: 잘못된 승인 입력이면 SG4 에서 계속 대기")
