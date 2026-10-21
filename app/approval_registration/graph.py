"""
approval_registration 서브그래프 (SG4 승인·등록)

흐름: prepare_approval_card -> request_approval(멈춤) -> recheck_supply -> register_quote

- 승인·반려 기록은 백엔드가 저장한다 (POST /api/quotes/{id}/approvals).
  SG4는 백엔드가 resume 해 줄 때까지 멈춰 있다가, 깨어나면 재확인과 등록만 한다.
- 반려되면 rejected / reject_reason 만 남기고 끝낸다. SG3 로 되돌리는 일은 상위 그래프(app/supervisor)가 한다.
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


MAX_INPUT_ATTEMPTS = 3  # 잘못된 승인 입력을 다시 받아 주는 횟수


def _parse_resume(resume_value: Any) -> tuple[bool, Optional[str]]:
    """resume 본문에서 (승인 여부, 반려 사유)를 읽는다. 잘못된 입력은 ValueError.

    승인으로 짐작하지 않는다: approved 가 정확히 true/false 일 때만 받고,
    반려에는 사유가 있어야 한다 (SG3 가 재작성에 쓴다). 승인자는 백엔드가 이미 기록했다.
    TODO: resume 요청 본문 형식 확정. 지금은 {"approved": true} 또는
    {"approved": false, "reject_reason": "..."} 로 가정한다.
    """
    if not isinstance(resume_value, dict):
        raise ValueError("승인 결과는 객체여야 합니다")
    approved = resume_value.get("approved")
    if not isinstance(approved, bool):
        raise ValueError("approved 는 true 또는 false 여야 합니다")
    if approved:
        return True, None
    reason = resume_value.get("reject_reason")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("반려에는 reject_reason(반려 사유)이 필요합니다")
    return False, reason.strip()


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
    # 반려 후 SG3 가 다시 작성해 들어오는 경우를 위해 이전 판정 결과를 비운다
    return {
        "approval_card": card,
        "approved": False,
        "rejected": False,
        "reject_reason": None,
        "recheck_ok": None,
        "sg4_outcome": None,
        "error": None,
    }


async def request_approval(state: SG4State) -> dict[str, Any]:
    """APPROVAL_REQUIRED 를 내보내고 멈춘다. 이 노드에는 부작용을 넣지 않는다.

    잘못된 입력이 오면 승인으로 보지 않고 같은 카드로 다시 멈춰 사유를 알린다.
    MAX_INPUT_ATTEMPTS 번 넘게 틀리면 INVALID_APPROVAL 로 끝낸다.
    """
    payload: dict[str, Any] = {
        "event": "APPROVAL_REQUIRED",  # TODO: SSE로 내보내는 위치(서버 쪽 변환) 확인
        "thread_id": state.thread_id,
        "card": state.approval_card.model_dump() if state.approval_card else None,
    }
    for _ in range(MAX_INPUT_ATTEMPTS):
        resume_value = interrupt(payload)
        try:
            approved, reject_reason = _parse_resume(resume_value)
        except ValueError as exc:
            logger.warning("SG4 잘못된 승인 입력 thread_id=%s: %s", state.thread_id, exc)
            payload = {**payload, "invalid_input": str(exc)}
            continue
        if approved:
            return {"approved": True, "rejected": False, "reject_reason": None}
        return {
            "approved": False,
            "rejected": True,
            "reject_reason": reject_reason,
            "sg4_outcome": "REWRITE_REQUIRED",
        }
    return {
        "approved": False,
        "sg4_outcome": "INVALID_APPROVAL",
        "error": "승인 입력이 올바르지 않아 중단했습니다",
    }


async def recheck_supply(state: SG4State) -> dict[str, Any]:
    """승인 직후, SG2 가 정한 최종 계획 수량으로 재고를 다시 조회한다.

    계획한 수량이 지금도 가용 재고 안에 있으면(상태 OK) 통과한다.
    대체 부품도 계획에 들어 있으면 일반 부품처럼 함께 확인한다.
    응답에 빠진 부품이 있으면 확인하지 못한 것이므로 실패로 본다.
    """
    plan = [{"part_id": pid, "required_qty": qty} for pid, qty in state.final_plan.items()]
    supply = await tools.business_check_parts_supply(state.due_date, plan, state.base_date)
    returned = {p.part_id: p for p in supply.parts}
    problems = [
        f"{pid} 확인 불가" for pid in state.final_plan if pid not in returned
    ] + [
        f"{p.part_id} 필요 {p.required_qty} / 가용 {p.available}"
        for p in supply.parts
        if p.status == "SHORT" or p.short_qty > 0
    ]
    update: dict[str, Any] = {"supply": supply, "recheck_ok": not problems}
    if problems:
        update["sg4_outcome"] = "STOCK_REVIEW_REQUIRED"
        # TODO: 재고가 부족해졌을 때 처리 방식 확정 (지금은 사유만 남기고 종료)
        update["error"] = "재고 변동: " + ", ".join(problems)
        logger.warning("SG4 재고 재확인 실패 thread_id=%s %s", state.thread_id, update["error"])
    return update


async def register_quote(state: SG4State) -> dict[str, Any]:
    """견적을 등록한다. 성공하면 그래프가 끝나고 DONE 이벤트가 나간다.

    승인 여부는 백엔드가 확인한다(승인되지 않은 견적이면 409). 중복 등록 방지 키는
    business-mcp 서버가 붙인다.
    """
    result = await tools.business_register_quote(state.quote_id, state.thread_id)
    return {
        "registered_quote_id": result.quote_id,
        "registration_status": result.status,
        "sg4_outcome": "REGISTERED",
    }


def _after_approval(state: SG4State) -> str:
    return "recheck_supply" if state.approved else END


def _after_recheck(state: SG4State) -> str:
    return "register_quote" if state.recheck_ok else END


def build_graph() -> StateGraph:
    """SG4 그래프를 만든다. 호출하는 쪽에서 checkpointer를 붙여 compile 한다."""
    graph = StateGraph(SG4State)
    graph.add_node("prepare_approval_card", prepare_approval_card)
    graph.add_node("request_approval", request_approval)
    graph.add_node("recheck_supply", recheck_supply)
    graph.add_node("register_quote", register_quote)

    graph.add_edge(START, "prepare_approval_card")
    graph.add_edge("prepare_approval_card", "request_approval")
    graph.add_conditional_edges("request_approval", _after_approval, ["recheck_supply", END])
    graph.add_conditional_edges("recheck_supply", _after_recheck, ["register_quote", END])
    graph.add_edge("register_quote", END)
    return graph
