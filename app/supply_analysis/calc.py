"""SG2 수량·금액·날짜와 추천 순위. 외부 호출이나 LLM에 의존하지 않는다."""

from collections import Counter
from datetime import date, timedelta
from itertools import product
from math import prod

from app.supply_analysis.rules import (
    Criteria,
    CriteriaError,
    evidence,
    penalty_policy,
    purchase_policies,
    substitute_rule,
)
from app.supply_analysis.schemas import (
    Alternative,
    BomPart,
    Criterion,
    DocHit,
    SelectedAlternative,
    Shortage,
    SubstitutesResult,
    SupplyPart,
    SupplyResult,
)

KINDS = ("WAIT_INCOMING", "PARTIAL_DELIVERY", "SUBSTITUTE_PART", "NEW_ORDER")
QUALITY_RANK = {"LOW": 0, "VALIDATION_REQUIRED": 1, "UNKNOWN": 2}


def shortages_from(supply: SupplyResult) -> list[Shortage]:
    return [
        Shortage(
            part=p.part_id, needed=p.required_qty, available=p.available,
            short_qty=p.short_qty,
            eta_days=max(0, (p.incoming_date - supply.base_date).days) if p.incoming_date else None,
        )
        for p in sorted(supply.parts, key=lambda p: p.part_id) if p.short_qty
    ]


def has_penalty_conflict(risks: list[dict]) -> bool:
    return any(
        risk.get("field") == "contract.penalty_cap_pct"
        or "지연 배상 상한 없음" in str(risk.get("item", ""))
        for risk in risks
    )


def build_alternatives(
    part: SupplyPart,
    substitutes: SubstitutesResult,
    base_date: date,
    due_date: date,
    criteria: Criteria,
    alt_docs: list[DocHit],
    policy_docs: list[DocHit],
    contract_risks: list[dict],
) -> list[Alternative]:
    """각 원 부품의 4종을 계산한다. 대체 후보가 여러 개면 내부 비교용으로 모두 보존."""
    common = {
        "part_id": part.part_id,
        "extra_cost_krw": 0,
        "validation_days": 0,
        "quality_risk": "LOW",
        "evidence": evidence([criteria.comparison]),
    }
    incoming_known = (
        part.incoming_date is not None
        and part.incoming_date >= base_date
        and part.incoming_qty is not None
        and part.incoming_qty >= part.short_qty
        and part.incoming_before_due is not None
        and part.incoming_delay_days is not None
    )
    incoming_on_time = incoming_known and part.incoming_before_due
    wait = Alternative(
        **common, type="WAIT_INCOMING", feasible=incoming_known,
        meets_due_date=bool(incoming_on_time), delay_days=part.incoming_delay_days,
        on_time_qty=part.required_qty if incoming_on_time else 0,
        delayed_qty=0 if incoming_on_time else part.required_qty,
        usable_date=part.incoming_date,
        reason=f"기존 입고 대기: {part.incoming_delay_days}일 지연" if incoming_known else "입고 일정 또는 입고 수량을 확인할 수 없습니다",
    )
    conflict = has_penalty_conflict(contract_risks)
    penalty_doc = penalty_policy(policy_docs)
    partial_constraints = []
    if conflict:
        partial_constraints.append("지연 배상 상한 없음 또는 상한 초과: 고객과 계약 조건 협의 필요")
    if penalty_doc is None:
        partial_constraints.append("지연 배상 규정 제13조 근거 확인 필요")
    if not incoming_known:
        partial_constraints.append("지연분 입고 일정·수량 확인 필요")
    partial = Alternative(
        **{**common, "evidence": evidence([criteria.comparison] + ([penalty_doc] if penalty_doc else []))},
        type="PARTIAL_DELIVERY", feasible=incoming_known and part.available > 0 and not partial_constraints,
        meets_due_date=bool(incoming_on_time), delay_days=part.incoming_delay_days,
        on_time_qty=part.required_qty if incoming_on_time else part.available,
        delayed_qty=0 if incoming_on_time else part.short_qty, usable_date=part.incoming_date,
        reason=f"정시 {part.available}개, 잔여 {part.short_qty}개는 입고 후 납품",
        constraints=partial_constraints,
    )
    arrival = part.new_order_arrival
    order_known = arrival is not None and arrival >= base_date
    delay = max(0, (arrival - due_date).days) if arrival else None
    order_on_time = order_known and delay == 0
    order = Alternative(
        **common, type="NEW_ORDER", feasible=order_known,
        meets_due_date=order_on_time, delay_days=delay,
        on_time_qty=part.required_qty if order_on_time else 0,
        delayed_qty=0 if order_on_time else part.required_qty, usable_date=arrival,
        reason=f"원 공급사 신규 발주: {delay}일 지연" if order_known else "신규 발주 도착일 확인 필요",
    )
    substitute_alternatives = []
    for candidate in substitutes.substitutes:
        blockers = []
        refs = [criteria.comparison]
        if criteria.eligibility is None:
            blockers.append("RAG 대체 부품 검토 조건(1절)을 확인하지 못했습니다")
        else:
            refs.append(criteria.eligibility)
        if part.incoming_date is None or part.incoming_date <= due_date:
            blockers.append("입고 예정일이 고객 납기보다 늦다는 대체 검토 조건을 충족하지 않습니다")
        if candidate.available < part.short_qty or candidate.covers_shortage is False:
            blockers.append("대체 부품 가용 수량이 부족합니다")
        if not candidate.pin_compatible:
            blockers.append("핀 호환 조건을 충족하지 않습니다")
        if candidate.substitute_part_id == part.part_id:
            blockers.append("원 부품과 대체 부품 ID가 같습니다")
        if candidate.unit_cost_krw - substitutes.unit_cost_krw != candidate.unit_cost_diff_krw:
            blockers.append("대체 부품 단가 차이가 원 부품·대체 부품 단가와 일치하지 않습니다")
        try:
            required_days, rule_doc = substitute_rule(part.part_id, candidate, alt_docs)
            refs.append(rule_doc)
            if candidate.validation_days < required_days:
                blockers.append("MCP 검증 기간이 RAG 기준보다 짧습니다")
            if candidate.usable_date < base_date + timedelta(days=candidate.validation_days):
                blockers.append("사용 가능일이 검증 완료일보다 빠릅니다")
        except CriteriaError as exc:
            blockers.append(str(exc))
        try:
            refs.extend(purchase_policies(policy_docs))
        except CriteriaError as exc:
            blockers.append(str(exc))
        on_time = candidate.usable_date <= due_date
        substitute_alternatives.append(Alternative(
            **{**common, "evidence": evidence(refs),
               "extra_cost_krw": part.short_qty * candidate.unit_cost_diff_krw,
               "validation_days": candidate.validation_days,
               "quality_risk": "UNKNOWN" if blockers else "VALIDATION_REQUIRED"},
            type="SUBSTITUTE_PART", feasible=not blockers,
            meets_due_date=on_time and not blockers,
            delay_days=max(0, (candidate.usable_date - due_date).days),
            on_time_qty=part.required_qty if on_time and not blockers else part.available,
            delayed_qty=0 if on_time and not blockers else part.short_qty,
            substitute_part_id=candidate.substitute_part_id, substitute_supplier=candidate.supplier,
            usable_date=candidate.usable_date,
            reason="; ".join(blockers) if blockers else f"검증 {candidate.validation_days}일 후 대체 부품 {part.short_qty}개 적용, 추가 비용 {part.short_qty * candidate.unit_cost_diff_krw:,}원",
            constraints=blockers or ["검증 시험 통과 후 생산 투입", "대체 적용 사실과 추가 비용을 견적 조건에 별도 명시"],
        ))
    if not substitute_alternatives:
        substitute_alternatives = [Alternative(
            **{**common, "quality_risk": "UNKNOWN"}, type="SUBSTITUTE_PART", feasible=False,
            meets_due_date=False, delay_days=None, on_time_qty=part.available,
            delayed_qty=part.short_qty, reason="등록된 대체 부품 후보가 없습니다",
        )]
    return [wait, partial, *substitute_alternatives, order]


def rank(alternative: Alternative | SelectedAlternative, criteria: list[Criterion]) -> tuple:
    values = {
        "meets_due_date": not alternative.meets_due_date,
        "extra_cost_krw": alternative.extra_cost_krw,
        "validation_days": alternative.validation_days,
        "quality_risk": QUALITY_RANK[alternative.quality_risk],
    }
    return tuple(values[key] for key in criteria)


def select_plan(
    candidates: dict[str, list[Alternative]],
    bom: list[BomPart],
    substitutes: dict[str, SubstitutesResult],
    supply: SupplyResult,
    criteria: list[Criterion],
) -> tuple[list[Alternative], SelectedAlternative | None]:
    """전체 부족분을 충족하는 같은 종류의 대안을 비교한다.

    DB recommended_alt가 단일 enum이므로 서로 다른 종류를 묵시적으로 섞지 않는다.
    대체 후보 조합은 공동 재고를 중복 배정하지 않는 조합 중에서 비교한다.
    final_plan은 변경 없는 부품도 포함하며, 전량 대체되어 수량이 0인 부품은 제외한다.
    """
    baseline = {p.part_id: p.required_qty for p in bom}
    short = {p.part_id: p.short_qty for p in supply.parts if p.short_qty}
    stocks = {}
    for result in substitutes.values():
        for candidate in result.substitutes:
            key = candidate.substitute_part_id
            stocks[key] = min(stocks.get(key, candidate.available), candidate.available)
    part_ids = sorted(candidates)
    plans = []
    for kind in KINDS:
        choices = [[a for a in candidates[pid] if a.type == kind and a.feasible] for pid in part_ids]
        # 거대한 조합 탐색을 무한히 수행하거나 일부만 보고 최적이라고 주장하지 않는다.
        if prod(len(items) for items in choices) > 10000:
            raise CriteriaError("대체 후보 조합이 10,000개를 초과하여 후보 범위를 좁혀야 합니다")
        best = None
        for combination in product(*choices):
            full_plan = dict(baseline)
            if kind == "SUBSTITUTE_PART":
                allocated = Counter()
                for alt in combination:
                    allocated[alt.substitute_part_id] += short[alt.part_id]
                if any(qty + baseline.get(pid, 0) > stocks[pid] for pid, qty in allocated.items()):
                    continue
                for alt in combination:
                    full_plan[alt.part_id] -= short[alt.part_id]
                for pid, qty in allocated.items():
                    full_plan[pid] = full_plan.get(pid, 0) + qty
            plan = SelectedAlternative(
                type=kind, parts=list(combination),
                meets_due_date=all(a.meets_due_date for a in combination),
                extra_cost_krw=sum(a.extra_cost_krw for a in combination),
                validation_days=max(a.validation_days for a in combination),
                quality_risk=max((a.quality_risk for a in combination), key=QUALITY_RANK.get),
                final_plan={pid: qty for pid, qty in sorted(full_plan.items()) if qty > 0},
                reason="RAG 대안 비교 기준 순서로 전체 부족분의 납기·원가·검증·품질 위험을 비교했습니다",
            )
            if best is None or rank(plan, criteria) < rank(best, criteria):
                best = plan
        if best:
            plans.append(best)
    selected = min(plans, key=lambda p: (rank(p, criteria), KINDS.index(p.type))) if plans else None
    preferred = {a.part_id: a for a in selected.parts} if selected else {}
    visible = []
    for pid in part_ids:
        for kind in KINDS:
            choices = [a for a in candidates[pid] if a.type == kind]
            chosen = min(choices, key=lambda a: (not a.feasible, rank(a, criteria), a.substitute_part_id or ""))
            if pid in preferred and preferred[pid].type == kind:
                chosen = preferred[pid]
            visible.append(chosen)
    return visible, selected
