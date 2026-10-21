"""get_parts → check_supply → [build_alternatives → recommend] → save."""

import asyncio
from functools import wraps

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.supply_analysis import deps
from app.supply_analysis.calc import build_alternatives as calculate_alternatives
from app.supply_analysis.calc import select_plan, shortages_from
from app.supply_analysis.mcp_tools import ToolError
from app.supply_analysis.rules import CriteriaError, read_criteria
from app.supply_analysis.schemas import (
    BomPart, BomResult, DocHit, SG2State, SubstitutesResult,
    SupplyAnalysisResult, SupplyResult,
)
from app.supply_analysis.service import normalize_input
from app.supply_analysis.tracing import traced_node


def hold(reason: str) -> dict:
    return {
        "next_step": "hold", "status": "ON_HOLD", "error": reason, "saved": False,
        "final_plan": {}, "recommended_alt": None, "selected_alternative": None,
    }


def fail_on_error(function):
    @wraps(function)
    async def wrapper(state):
        try:
            return await function(state)
        except CriteriaError as exc:
            return hold(f"{function.__name__}: {exc}")
        except (ToolError, ValueError) as exc:
            message = str(exc.errors(include_input=False, include_url=False)) if isinstance(exc, ValidationError) else str(exc)
            result = {**hold(f"{function.__name__}: {message}"), "status": "FAILED"}
            if function.__name__ == "get_parts":
                result = {**SupplyAnalysisResult().model_dump(mode="json"), **result}
            return result

    return wrapper


@traced_node("supply_analysis", "get_parts")
@fail_on_error
async def get_parts(state: SG2State) -> dict:
    request = normalize_input(state)
    raw = await deps.gateway().call(
        "business_get_bom", model_id=request.selected_model,
        options=request.options, qty=request.demand.sample_qty,
    )
    result = BomResult.model_validate(raw)
    if result.model_id != request.selected_model or set(result.options) != set(request.options) or result.qty != request.demand.sample_qty:
        raise ValueError("BOM 응답의 모델·옵션·수량이 SG1 요청과 다릅니다")
    if len({p.part_id for p in result.parts}) != len(result.parts):
        raise ValueError("BOM에 중복된 part_id가 있습니다")
    if any(p.required_qty != p.qty_per_unit * request.demand.sample_qty for p in result.parts):
        raise ValueError("BOM 필요 수량이 단위 수량 × 주문 수량과 다릅니다")
    bom = [p.model_dump(mode="json", exclude_none=True) for p in sorted(result.parts, key=lambda p: p.part_id)]
    return {
        **SupplyAnalysisResult().model_dump(mode="json"),
        "request": request, "bom": bom, "bom_effective": bom,
        "review_id": request.review_id, "status": "ANALYZING", "next_step": "",
        "candidates": {}, "substitutes": {}, "criteria": [],
    }


@traced_node("supply_analysis", "check_supply")
@fail_on_error
async def check_supply(state: SG2State) -> dict:
    request = state["request"]
    arguments = {
        "due_date": request.demand.sample_due_date.isoformat(),
        "parts": [{"part_id": p["part_id"], "required_qty": p["required_qty"]} for p in state["bom"]],
    }
    if request.base_date:
        arguments["base_date"] = request.base_date.isoformat()
    supply = SupplyResult.model_validate(await deps.gateway().call("business_check_parts_supply", **arguments))
    if supply.due_date != request.demand.sample_due_date or (request.base_date and supply.base_date != request.base_date):
        raise ValueError("재고 확인 응답의 납기·기준일이 요청과 다릅니다")
    if supply.base_date > supply.due_date:
        raise ValueError("고객 납기가 검토 기준일보다 과거입니다")
    expected = {p["part_id"]: p["required_qty"] for p in state["bom"]}
    if len(supply.parts) != len(expected) or {p.part_id: p.required_qty for p in supply.parts} != expected:
        raise ValueError("재고 확인 응답의 부품·필요 수량이 BOM과 다릅니다")
    for part in supply.parts:
        if part.incoming_date:
            if part.incoming_before_due is not None and part.incoming_before_due != (part.incoming_date <= supply.due_date):
                raise ValueError(f"{part.part_id}: 입고일과 납기 충족 표시가 다릅니다")
            if part.incoming_delay_days is not None and part.incoming_delay_days != max(0, (part.incoming_date - supply.due_date).days):
                raise ValueError(f"{part.part_id}: 입고일과 지연일이 다릅니다")
    shortages = [p.model_dump(mode="json") for p in shortages_from(supply)]
    return {
        "supply_check": supply.model_dump(mode="json"), "shortages": shortages,
        "final_plan": {} if shortages else {
            part["part_id"]: part["required_qty"] for part in state["bom"]
        },
    }


@traced_node("supply_analysis", "build_alternatives")
@fail_on_error
async def build_alternatives(state: SG2State) -> dict:
    request = state["request"]
    supply = SupplyResult.model_validate(state["supply_check"])
    shortage_parts = sorted((p for p in supply.parts if p.short_qty), key=lambda p: p.part_id)
    gateway = deps.gateway()

    async def search(query: str, doc_type: str) -> list[DocHit]:
        raw = await gateway.call("rag_search_docs", query=query, doc_type=doc_type, k=10)
        if not isinstance(raw, list):
            raise ValueError("RAG 검색 결과는 청크 배열이어야 합니다")
        return [DocHit.model_validate(item) for item in raw]

    alt_docs, policy_docs = await asyncio.gather(
        search("대체 부품 적용 기준 검토 조건 성능 등급 대안 비교 기준 납기 추가 원가 검증 기간 품질 위험", "alt_parts"),
        search("제13조 지연 배상 상한 제20조 신규 공급사 검증 시험 제21조 대체 부품 추가 원가", "policy"),
    )
    criteria = read_criteria(alt_docs)
    candidates = {}
    substitutes = {}
    for part in shortage_parts:
        result = SubstitutesResult.model_validate(await gateway.call(
            "business_get_substitutes", part_id=part.part_id,
            short_qty=part.short_qty, base_date=supply.base_date.isoformat(),
        ))
        if result.part_id != part.part_id:
            raise ValueError("대체 후보 응답의 원 부품 ID가 요청과 다릅니다")
        ids = [candidate.substitute_part_id for candidate in result.substitutes]
        if len(set(ids)) != len(ids):
            raise ValueError("대체 후보에 중복된 부품 ID가 있습니다")
        # 후보의 ref_chunk_id를 실제 문서와 대조. 검색 순서나 유사도 점수로 승인하지 않는다.
        docs = list(alt_docs)
        for candidate in result.substitutes:
            if not any(doc.chunk_id == candidate.ref_chunk_id for doc in docs):
                docs.extend(await search(
                    f"{part.part_id} {candidate.substitute_part_id} {candidate.supplier} {candidate.ref_chunk_id} 성능 등급 핀 배치 검증 시험",
                    "alt_parts",
                ))
        by_id = {}
        for doc in docs:
            if doc.chunk_id in by_id and by_id[doc.chunk_id].content != doc.content:
                raise ValueError("조회 도중 RAG 근거 문서가 변경되었습니다. 다시 분석해야 합니다")
            by_id[doc.chunk_id] = doc
        substitutes[part.part_id] = result
        candidates[part.part_id] = calculate_alternatives(
            part, result, supply.base_date, supply.due_date, criteria,
            list(by_id.values()), policy_docs, request.contract_risks,
        )
    return {"candidates": candidates, "substitutes": substitutes, "criteria": criteria.order}


@traced_node("supply_analysis", "recommend")
@fail_on_error
async def recommend(state: SG2State) -> dict:
    alternatives, selected = select_plan(
        state["candidates"], [BomPart.model_validate(p) for p in state["bom"]],
        state["substitutes"], SupplyResult.model_validate(state["supply_check"]), state["criteria"],
    )
    visible = [a.model_dump(mode="json") for a in alternatives]
    if selected is None:
        return {**hold("전체 부족분에 적용 가능한 공급 대안이 없습니다"), "alternatives": visible}
    if not selected.meets_due_date:
        return {**hold("모든 실행 가능한 대안이 납기를 넘깁니다. 납기 또는 계약 조건 협의가 필요합니다"), "alternatives": visible}
    return {
        "alternatives": visible, "selected_alternative": selected.model_dump(mode="json"),
        "recommended_alt": selected.type, "final_plan": selected.final_plan,
    }


@traced_node("supply_analysis", "save")
@fail_on_error
async def save(state: SG2State) -> dict:
    request = state["request"]
    on_hold = state.get("status") == "ON_HOLD" and bool(state.get("error"))
    payload = {
        "thread_id": request.thread_id, "rfq_number": request.rfq_number, "stage": "SG2",
        "status": "WAITING" if on_hold else "DONE", "shortages": state["shortages"], "alternatives": state["alternatives"],
        "final_plan": state["final_plan"],
    }
    if state.get("recommended_alt") is not None:
        payload["recommended_alt"] = state["recommended_alt"]
    result = await deps.gateway().call("business_save_review", **payload)
    if not isinstance(result, dict) or result.get("thread_id") != request.thread_id or not result.get("review_id"):
        raise ValueError("검토 저장 응답의 thread_id 또는 review_id를 확인할 수 없습니다")
    if on_hold:
        return {"review_id": result["review_id"], "saved": True, "next_step": "hold", "status": "ON_HOLD"}
    return {"review_id": result["review_id"], "saved": True, "next_step": "quote_drafting", "status": "DRAFTING", "error": None}


def build_graph() -> StateGraph:
    """상위 그래프가 필요에 따라 checkpointer를 붙여 compile한다."""
    graph = StateGraph(SG2State)
    for name, node in (("get_parts", get_parts), ("check_supply", check_supply),
                       ("build_alternatives", build_alternatives), ("recommend", recommend), ("save", save)):
        graph.add_node(name, node)
    graph.add_edge(START, "get_parts")
    graph.add_conditional_edges("get_parts", lambda s: END if s.get("error") else "check_supply")
    graph.add_conditional_edges("check_supply", lambda s: END if s.get("error") else ("build_alternatives" if s["shortages"] else "save"))
    graph.add_conditional_edges("build_alternatives", lambda s: (
        END if s.get("status") == "FAILED" else "save" if s.get("status") == "ON_HOLD" else "recommend"
    ))
    graph.add_conditional_edges("recommend", lambda s: END if s.get("status") == "FAILED" else "save")
    graph.add_edge("save", END)
    return graph
