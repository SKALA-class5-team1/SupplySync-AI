"""SG1 → SG2 → SG3 → SG4. 모든 interrupt는 부모의 동일 thread_id로 재개한다."""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.approval_registration.graph import build_graph as build_sg4
from app.quote_drafting.graph import build_graph as build_sg3
from app.quote_drafting.harness import MAX_REVISION
from app.rfq_review.graph import build_graph as build_sg1
from app.supply_analysis.graph import build_graph as build_sg2
from app.supply_analysis.plan import validate_final_plan
from app.supply_analysis.schemas import SupplyAnalysisResult
from app.supervisor.state import SupervisorState


def _hold(reason: str) -> dict:
    return {"status": "ON_HOLD", "next_step": "hold", "error": reason}


def _failed(reason: str) -> dict:
    return {"status": "FAILED", "next_step": "hold", "error": reason}


def begin_review(state: SupervisorState) -> dict:
    return {
        "current_stage": "SG1", "status": "ANALYZING", "next_step": "rfq_review",
        "draft_revision": 1, "quote_ids": [], "final_plan": {}, "error": None,
    }


def after_review(state: SupervisorState) -> dict:
    if not state.get("selected_model") or state.get("fit_type") == "신규 개발":
        return _hold("SG1에서 공급 분석 가능한 모델을 선정하지 못했습니다. 신규 개발 검토가 필요합니다")
    if not state.get("review_id"):
        return _failed("SG1 검토 결과 저장을 확인할 수 없습니다")
    return {
        "current_stage": "SG2",
        "status": "ANALYZING", "next_step": "supply_analysis",
    }


def after_supply(state: SupervisorState) -> dict:
    if state.get("status") == "FAILED":
        return _failed(state.get("error") or "SG2 처리 실패")
    if not state.get("saved") or state.get("next_step") != "quote_drafting":
        return _hold(state.get("error") or "SG2 공급 계획이 확정되지 않았습니다")
    try:
        supply = SupplyAnalysisResult.model_validate(state)
        validate_final_plan(supply)
    except ValueError as exc:
        return {**_failed(f"SG2 인계 실패: {exc}"), "final_plan": {}}
    return {
        "supply_analysis": {**supply.model_dump(mode="json"), "demand": state["demand"]},
        "due_date": state["demand"]["sample_due_date"], "current_stage": "SG3",
        "status": "DRAFTING", "next_step": "quote_drafting", "error": None,
    }


def after_quote(state: SupervisorState) -> dict:
    if state.get("sg3_outcome") != "DRAFT_SAVED" or not state.get("quote_id"):
        return _failed(state.get("error") or "SG3 견적 저장을 확인할 수 없습니다")
    return {
        "current_stage": "SG4", "status": "APPROVING", "next_step": "approval_registration",
        "quote_ids": [*state.get("quote_ids", []), state["quote_id"]],
    }


def after_approval(state: SupervisorState) -> dict:
    outcome = state.get("sg4_outcome")
    if outcome == "REGISTERED":
        return {"status": "DONE", "next_step": "done", "error": None}
    if outcome == "REJECTED":
        return {"status": "REJECTED", "next_step": "done", "error": None}
    if outcome not in {"REWRITE_REQUIRED", "STOCK_REVIEW_REQUIRED"}:
        return _failed(state.get("error") or f"SG4 처리 중단: {outcome}")
    if state["draft_revision"] >= MAX_REVISION:
        return _hold(f"견적 재작성 한도({MAX_REVISION}차)에 도달했습니다. 담당자 검토가 필요합니다")
    resupply = outcome == "STOCK_REVIEW_REQUIRED"
    return {
        "draft_revision": state["draft_revision"] + 1,
        "current_stage": "SG2" if resupply else "SG3",
        "status": "ANALYZING" if resupply else "DRAFTING",
        "next_step": "supply_analysis" if resupply else "quote_drafting",
        "reject_reason": state.get("error") if resupply else state.get("reject_reason"),
        "changes": {} if resupply else state.get("changes", {}),
        "quote_id": None, "quote_draft": {}, "quote_summary": "", "approval_items": [],
        "evidence_ids": [], "sg3_outcome": None, "approval_card": None,
        "approved": False, "rejected": False, "recheck_ok": None, "error": None,
        **({"final_plan": {}, "saved": False, "supply_analysis": {}} if resupply else {}),
    }


def build_graph(*, checkpointer=None, quote_llm=None, quote_fallback_llm=None):
    """서브그래프가 부모 체크포인터를 상속한다. 저장소 교체는 호출자가 주입한다."""
    graph = StateGraph(SupervisorState)
    for name, node in (
        ("begin_review", begin_review), ("after_review", after_review),
        ("after_supply", after_supply), ("after_quote", after_quote), ("after_approval", after_approval),
        ("sg1", build_sg1(checkpointer=None)),
        ("sg2", build_sg2().compile()),
        ("sg3", build_sg3(llm=quote_llm, fallback_llm=quote_fallback_llm).compile()),
        ("sg4", build_sg4().compile()),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "begin_review")
    graph.add_edge("begin_review", "sg1")
    for stage, after in (("sg1", "after_review"), ("sg2", "after_supply"),
                         ("sg3", "after_quote"), ("sg4", "after_approval")):
        graph.add_edge(stage, after)
    graph.add_conditional_edges("after_review", lambda s: s["next_step"], {"supply_analysis": "sg2", "hold": END})
    graph.add_conditional_edges("after_supply", lambda s: s["next_step"], {"quote_drafting": "sg3", "hold": END})
    graph.add_conditional_edges("after_quote", lambda s: s["next_step"], {"approval_registration": "sg4", "hold": END})
    graph.add_conditional_edges("after_approval", lambda s: s["next_step"], {
        "quote_drafting": "sg3", "supply_analysis": "sg2", "hold": END, "done": END,
    })
    return graph.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver())
