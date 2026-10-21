"""단계 사이에 유지하는 State. 각 SG의 내부 작업 값은 해당 서브그래프에 둔다."""

from typing import Any

from app.rfq_review.state import SG1State


class SupervisorState(SG1State, total=False):
    current_stage: str
    status: str
    next_step: str
    error: str | None
    base_date: str | None
    issue_date: str | None
    rfq_id: str
    due_date: str

    # SG2: 원 BOM과 전체 최종 계획을 별도로 보존한다.
    bom: list[dict]
    bom_effective: list[dict]
    supply_check: dict | None
    shortages: list[dict]
    alternatives: list[dict]
    selected_alternative: dict | None
    recommended_alt: str | None
    final_plan: dict[str, int]
    saved: bool
    supply_analysis: dict

    # SG3: 상태만 공유하며 모델 대화·도구 결과는 SG3 내부에 둔다.
    draft_revision: int
    quote_draft: dict
    quote_id: int | str | None
    quote_ids: list[int | str]
    quote_summary: str
    approval_items: list[str]
    evidence_ids: list[str]
    sg3_outcome: str | None
    reject_reason: str | None
    changes: dict

    # SG4: 승인·재고 확인·등록 결과
    approval_card: Any
    approved: bool
    rejected: bool
    decision_result: str | None
    approver_id: int | str | None
    supply: Any
    recheck_ok: bool | None
    sg4_outcome: str | None
    registered_quote_id: int | str | None
    registration_status: str | None
    registration_outcome: str | None
