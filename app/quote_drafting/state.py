"""SG3 서브그래프 State"""
from typing import NotRequired, TypedDict


class QuoteDraftingState(TypedDict):
    # 입력 (SG1·SG2·SG4)
    thread_id: str
    review_id: int
    parsed_rfq: dict  # SG1 parse_rfq 결과 (05_expected/1_parsed_rfq.json)
    rfq_review: dict  # SG1 → SG2·SG3 state (05_expected/4_output_state.json)
    supply_analysis: dict  # SG2 결과 (05_expected/5_sg2_supply.json)
    issue_date: NotRequired[str]  # 발행일 (YYYY-MM-DD). 없으면 오늘
    reject_reason: NotRequired[str | None]  # SG4 반려 사유. 있으면 문구 재작성에 반영

    # 작업
    quote_draft: NotRequired[dict]  # 05_expected/6_sg3_quote.json의 quote_draft 구조
    condition_facts: NotRequired[list[dict]]  # conditions와 같은 순서. 문구에 쓸 수 있는 사실(숫자 검증 기준)
    feedback: NotRequired[list[str]]  # 검증 실패 내용. 재작성 시 writer에 전달
    attempts: NotRequired[int]  # draft_quote 실행 횟수

    # 출력
    quote_id: NotRequired[str | None]
    status: NotRequired[str]  # DRAFT | FAILED
    error: NotRequired[str | None]
