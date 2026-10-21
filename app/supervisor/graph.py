"""
상위 그래프(Supervisor) 초안 - SG1 -> SG2 -> SG3 -> SG4, 반려되면 SG3 로 되돌아간다.

!! SG1~SG3 노드는 임시(가짜)다. 실제 서브그래프가 만들어지면 같은 이름의 노드를 갈아 끼운다.
   SG4 만 실제 서브그래프(app/approval_registration)를 쓴다.

- 이 파일은 다른 도메인 폴더·라우터·main.py 를 건드리지 않는다.
- 호출하는 쪽이 checkpointer 를 붙여 compile 한다. SG4 의 interrupt 는 그 checkpointer 로 멈추고 이어진다.
- LLM 이 고르는 Supervisor 가 아니라, 정해진 순서와 조건(반려·재고 변동)만 다루는 단순한 흐름이다.
"""
import logging
from typing import Any, Optional, Union

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app.approval_registration.graph import build_graph as build_sg4_graph
from app.approval_registration.schemas import ApprovalCard, Sg4Outcome, SupplyResult

logger = logging.getLogger(__name__)

MAX_REVISIONS = 3  # 반려 후 재작성 허용 횟수 (무한 반복 방지)


class SupervisorState(BaseModel):
    """상위 그래프 상태. SG4 가 읽고 쓰는 키는 SG4State 와 이름·타입을 맞췄다."""

    # --- 실행 시작 때 받는 값 ---
    thread_id: str
    rfq_id: Union[int, str]
    rfq_number: str = Field(default="", examples=["HB-EV7-EWP-001"])

    # --- SG2 가 채우는 값 ---
    final_plan: dict[str, int] = Field(default_factory=dict)
    due_date: Optional[str] = None
    base_date: Optional[str] = None

    # --- SG3 가 채우는 값 ---
    quote_id: Optional[Union[int, str]] = None
    quote_summary: str = ""
    approval_items: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    draft_revision: int = 0

    # --- SG4 가 채우는 값 ---
    approval_card: Optional[ApprovalCard] = None
    approved: bool = False
    rejected: bool = False
    decision_result: Optional[str] = None
    approver_id: Optional[Union[int, str]] = None
    changes: dict[str, Any] = Field(default_factory=dict)
    reject_reason: Optional[str] = None
    supply: Optional[SupplyResult] = None
    recheck_ok: Optional[bool] = None
    sg4_outcome: Optional[Sg4Outcome] = None
    registered_quote_id: Optional[Union[int, str]] = None
    registration_status: Optional[str] = None
    error: Optional[str] = None


# ----------------------------------------------------------------------
# 임시 노드 (SG1~SG3). 데이터셋 v2.2 의 정답 값으로 채운다.
# ----------------------------------------------------------------------
async def sg1_rfq_review(state: SupervisorState) -> dict[str, Any]:
    """TODO: 실제 SG1(rfq_review) 서브그래프로 교체."""
    return {"rfq_number": state.rfq_number or "HB-EV7-EWP-001"}


async def sg2_supply_analysis(state: SupervisorState) -> dict[str, Any]:
    """TODO: 실제 SG2(supply_analysis) 서브그래프로 교체. 최종 조달 계획을 SG4 에 넘긴다."""
    return {
        "final_plan": {"P-MCU-G1": 1500, "P-MCU-G1-ALT": 500},
        "due_date": "2026-12-15",
        "base_date": "2026-10-19",
    }


async def sg3_quote_drafting(state: SupervisorState) -> dict[str, Any]:
    """TODO: 실제 SG3(quote_drafting) 서브그래프로 교체.

    견적 초안을 DRAFT 로 저장하고 quote_id 를 돌려주는 자리다. 반려로 돌아왔다면
    state.reject_reason 을 반영해 다시 작성해야 한다.
    """
    revision = state.draft_revision + 1
    summary = "EWP-12-B + OPT-HT + OPT-IP9K, 단가 37,300원"
    if state.reject_reason:
        summary += f" (수정 요청 반영: {state.reject_reason})"
    return {
        "quote_id": "Q-2026-1019-001",
        "quote_summary": summary,
        "approval_items": ["보증 5년 수용 시 품질본부장 승인 필요", "시작품 500개 대체 칩 적용"],
        "draft_revision": revision,
    }


# ----------------------------------------------------------------------
# 분기
# ----------------------------------------------------------------------
def _after_sg4(state: SupervisorState) -> str:
    """SG4 결과에 따라 갈 곳을 정한다. 이동은 규칙으로만 정하고 LLM 판단은 쓰지 않는다."""
    if state.sg4_outcome == "REWRITE_REQUIRED":
        if state.draft_revision < MAX_REVISIONS:
            return "sg3_quote_drafting"
        logger.warning("반려가 %d회를 넘어 종료 thread_id=%s", MAX_REVISIONS, state.thread_id)
    # REJECTED: 최종 반려. SG3 로 돌리지 않고 종료한다 (결과값 REJECTED 를 종료 이벤트에 담는다).
    # REGISTERED: 전체 완료(DONE).
    # STOCK_REVIEW_REQUIRED: DONE 이 아니다. 후속 정책(SG2 복귀 등)은 팀 합의 전이라 여기서 멈춘다.
    # INVALID_APPROVAL: 승인 입력 오류로 중단.
    # FAILED: MCP 호출이 재시도 후에도 실패. DONE 이 아니다. 실행 상태 FAILED 처리는 서버(실행 서비스)에서 한다.
    return END


def build_supervisor_graph() -> StateGraph:
    """상위 그래프를 만든다. 호출하는 쪽에서 checkpointer 를 붙여 compile 한다."""
    graph = StateGraph(SupervisorState)
    graph.add_node("sg1_rfq_review", sg1_rfq_review)
    graph.add_node("sg2_supply_analysis", sg2_supply_analysis)
    graph.add_node("sg3_quote_drafting", sg3_quote_drafting)
    graph.add_node("sg4_approval_registration", build_sg4_graph().compile())

    graph.add_edge(START, "sg1_rfq_review")
    graph.add_edge("sg1_rfq_review", "sg2_supply_analysis")
    graph.add_edge("sg2_supply_analysis", "sg3_quote_drafting")
    graph.add_edge("sg3_quote_drafting", "sg4_approval_registration")
    graph.add_conditional_edges(
        "sg4_approval_registration", _after_sg4, ["sg3_quote_drafting", END]
    )
    return graph
