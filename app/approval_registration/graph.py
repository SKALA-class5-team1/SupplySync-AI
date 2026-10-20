"""
approval_registration 서브그래프 (SG4 승인·등록)

흐름: prepare_approval_card -> request_approval(멈춤) -> recheck_atp -> register_quote

- 승인·반려 기록은 백엔드가 저장한다 (POST /api/quotes/{id}/approvals).
  SG4는 백엔드가 resume 해 줄 때까지 멈춰 있다가, 깨어나면 재확인과 등록만 한다.
- 반려는 다루지 않는다 (API 흐름 문서: 승인만).
- TODO 표시는 팀과 확정이 필요한 부분이다.
- interrupt 는 Python 3.11 이상에서만 동작한다 (async 그래프).
"""
import logging
from typing import Any, Optional

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.approval_registration import tools
from app.approval_registration.schemas import ApprovalCard, SG4State

logger = logging.getLogger(__name__)


def _parse_resume(resume_value: Any) -> tuple[bool, Optional[int]]:
    """resume 본문에서 (승인 여부, 승인자 ID)를 읽는다.

    TODO: resume 요청 본문 형식 확정. 지금은 {"approved": true, "approved_by": 2} 로 가정한다.
    """
    if isinstance(resume_value, dict):
        return bool(resume_value.get("approved", True)), resume_value.get("approved_by")
    return True, None


async def prepare_approval_card(state: SG4State) -> dict[str, Any]:
    """승인 카드를 만든다. 규정 근거는 rag_search_docs 로 찾는다.

    interrupt 노드는 resume 때 처음부터 다시 실행되므로, 외부 호출은 이 노드로 분리했다.
    근거 검색이 실패해도 승인 흐름은 막지 않는다.
    """
    evidence = []
    query = " ".join(state.approval_items).strip()
    if query:
        try:
            evidence = await tools.rag_search_docs(query, doc_type="policy", k=4)
        except Exception:  # noqa: BLE001 - 근거 검색 실패가 승인 요청을 막으면 안 된다
            logger.warning("SG4 근거 검색 실패 thread_id=%s", state.thread_id, exc_info=True)
    card = ApprovalCard(
        quote_id=state.quote_id,
        summary=state.quote_summary,
        approval_items=state.approval_items,
        evidence=evidence,
    )
    return {"approval_card": card}


async def request_approval(state: SG4State) -> dict[str, Any]:
    """APPROVAL_REQUIRED 를 내보내고 멈춘다. 이 노드에는 부작용을 넣지 않는다."""
    resume_value = interrupt(
        {
            "event": "APPROVAL_REQUIRED",  # TODO: SSE로 내보내는 위치(서버 쪽 변환) 확인
            "thread_id": state.thread_id,
            "card": state.approval_card.model_dump() if state.approval_card else None,
        }
    )
    approved, approved_by = _parse_resume(resume_value)
    update: dict[str, Any] = {"approved": approved, "approved_by": approved_by}
    if approved and approved_by is None:
        update["error"] = "승인자 정보(approved_by)가 resume 본문에 없습니다"
    return update


async def recheck_atp(state: SG4State) -> dict[str, Any]:
    """승인 직후 재고를 다시 조회한다.

    SG2 때 확인한 부품별 가용 수량(sg2_available)과 지금 가용 수량을 비교해,
    줄어든 부품이 있으면 등록하지 않는다. (정답 파일 7_sg4_register.json 의 recheck 방식)
    대체 부품이 충분한지는 SG2 가 계획을 세울 때 판단한 일이라 SG4 는 보지 않는다.
    ATP 응답에 없는 부품은 비교하지 않는다.
    """
    atp = await tools.business_get_atp(state.model_id, state.options, state.qty, state.due_date)
    now = {p.part_id: p.available for p in atp.parts}
    worse = [
        (e.part_id, e.available_at_sg2, now[e.part_id])
        for e in state.sg2_available
        if e.part_id in now and now[e.part_id] < e.available_at_sg2
    ]
    update: dict[str, Any] = {"atp": atp, "atp_ok": not worse}
    if worse:
        # TODO: 재고가 줄었을 때 처리 방식 확정 (지금은 사유만 남기고 종료)
        update["error"] = "재고 변동: " + ", ".join(
            f"{pid} 가용 {before} -> {after}" for pid, before, after in worse
        )
        logger.warning("SG4 재고 재확인 실패 thread_id=%s %s", state.thread_id, update["error"])
    return update


async def register_quote(state: SG4State) -> dict[str, Any]:
    """견적을 등록한다. 성공하면 그래프가 끝나고 DONE 이벤트가 나간다."""
    # TODO: 멱등 키 형식 확정 (같은 견적이면 항상 같은 값이어야 재시도가 안전하다)
    idempotency_key = f"{state.thread_id}:register_quote:{state.quote_id}"
    result = await tools.business_register_quote(
        state.quote_id,
        state.approved_by,
        thread_id=state.thread_id,
        idempotency_key=idempotency_key,
    )
    return {"registered_quote_id": result.quote_id, "registration_status": result.status}


def _after_approval(state: SG4State) -> str:
    return "recheck_atp" if state.approved and state.error is None else END


def _after_recheck(state: SG4State) -> str:
    return "register_quote" if state.atp_ok else END


def build_graph() -> StateGraph:
    """SG4 그래프를 만든다. 호출하는 쪽에서 checkpointer를 붙여 compile 한다."""
    graph = StateGraph(SG4State)
    graph.add_node("prepare_approval_card", prepare_approval_card)
    graph.add_node("request_approval", request_approval)
    graph.add_node("recheck_atp", recheck_atp)
    graph.add_node("register_quote", register_quote)

    graph.add_edge(START, "prepare_approval_card")
    graph.add_edge("prepare_approval_card", "request_approval")
    graph.add_conditional_edges("request_approval", _after_approval, ["recheck_atp", END])
    graph.add_conditional_edges("recheck_atp", _after_recheck, ["register_quote", END])
    graph.add_edge("register_quote", END)
    return graph
