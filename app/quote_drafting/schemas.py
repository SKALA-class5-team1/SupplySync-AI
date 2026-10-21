"""
quote_drafting 도메인 스키마 정의 (SG3 견적 작성)

SG3 흐름: 단가·원가 계산 -> 조건 문구 작성(Agent Loop) -> 저장 승인(멈춤) -> 견적 초안 저장
MCP 도구 입출력 모델은 app/quote_drafting/CLAUDE.md "SG3가 쓰는 도구 입출력"(business-mcp 명세)을 따른다.
SG4와 주고받는 필드는 SG4 요청(연결 약속)과 상위 그래프 SupervisorState 이름을 따른다.
  SG3 -> SG4: quote_id, quote_summary, approval_items, evidence_ids, draft_revision
  SG4 -> SG3 (수정 요청 REVISE): 직전 quote_id, reject_reason, changes, draft_revision (+1 된 값)
"""
from typing import Any, Literal, Optional, Union

from langchain_core.messages import AnyMessage
from pydantic import BaseModel, Field

from app.quote_drafting.harness import MAX_REVISION


# ----------------------------------------------------------------------
# MCP 도구 입출력
# ----------------------------------------------------------------------
class OptionPrice(BaseModel):
    """get_options 응답의 옵션 한 줄. (그 밖의 필드는 쓰지 않아 무시한다)"""

    option_code: str = Field(examples=["OPT-HT"])
    add_price_krw: int = Field(description="옵션 추가 단가", examples=[2400])


class OptionsResult(BaseModel):
    """get_options 응답."""

    model_id: str = Field(examples=["EWP-12-B"])
    base_price_krw: int = Field(description="모델 기본 단가", examples=[34000])
    options: list[OptionPrice] = Field(default_factory=list, description="모델이 지원하는 옵션 전체")


class PartCost(BaseModel):
    """get_parts_cost 응답의 부품 한 줄."""

    part_id: str = Field(examples=["P-MCU-G1"])
    qty_per_unit: int = 1
    unit_cost_krw: Optional[int] = None
    cost_krw: int = Field(description="완제품 1개당 부품 원가", examples=[5300])


class PartsCostResult(BaseModel):
    """get_parts_cost 응답. 원가는 양산 기준 부품(대체 부품 미포함)."""

    model_id: str
    options: list[str] = Field(default_factory=list)
    parts_cost_krw: int = Field(description="옵션 반영 BOM 부품 원가 합계", examples=[21650])
    parts_cost_breakdown: list[PartCost] = Field(default_factory=list)


class SaveQuoteResult(BaseModel):
    """save_quote 응답."""

    quote_id: Union[int, str] = Field(description="백엔드가 채번한 견적 번호", examples=["Q-2026-1019-001"])
    status: str = Field(examples=["DRAFT"])
    review_id: Optional[int] = Field(default=None, description="견적이 연결된 검토 결과 ID", examples=[1])


class DocHit(BaseModel):
    """rag search_docs 응답의 문서 조각 한 건 (모델에 넣는 필드만)."""

    chunk_id: str = Field(examples=["pol-003"])
    section: str = Field(default="", examples=["제12조 품질 보증 기간"])
    content: str = ""


# ----------------------------------------------------------------------
# 그래프 상태
# ----------------------------------------------------------------------
class SaveApprovalCard(BaseModel):
    """SAVE_APPROVAL_REQUIRED 이벤트로 화면에 보내는 저장 승인 카드."""

    quote_summary: str = Field(default="", examples=["EWP-12-B + OPT-HT + OPT-IP9K, 단가 37,300원"])
    approval_items: list[str] = Field(default_factory=list, description="사람이 확인할 내용 (기준 미달·대체 부품·계약 수정 제안)")
    checks: list[dict[str, Any]] = Field(default_factory=list, description="검증 5항목 결과")


# SG3 가 상위 그래프에 돌려주는 결과
#   DRAFT_SAVED       견적 초안 저장 완료 -> SG4 승인으로
#   FAILED            한도 초과, 도구·모델 실패, 저장 거절(409 등)
#   SAVE_UNCONFIRMED  저장 호출의 응답을 못 받음. 실제로는 저장됐을 수 있어 사람이 확인해야 함
Sg3Outcome = Literal["DRAFT_SAVED", "FAILED", "SAVE_UNCONFIRMED"]


class SG3State(BaseModel):
    """SG3 서브그래프 상태."""

    # --- 앞 단계가 state에 넣어 둔 값. SG1 키는 SG1State(app/rfq_review/state.py)와 같은 이름 ---
    thread_id: str = Field(description="LangGraph 실행 ID (재개 키, trace_id)")
    rfq_number: str = Field(description="RFQ 번호 (SG1 입력)", examples=["HB-EV7-EWP-001"])
    review_id: Optional[int] = Field(
        default=None, description="SG1·SG2 검토 결과 ID (save_quote는 thread_id로 찾으므로 보내지 않는다)"
    )
    parsed_rfq: dict[str, Any] = Field(description="SG1 parse_rfq 결과 (ParsedRFQ.model_dump())")
    selected_model: Optional[str] = Field(
        default=None, description="SG1 match_spec이 고른 모델. 없으면(신규 개발 등) SG3는 FAILED", examples=["EWP-12-B"]
    )
    options: list[str] = Field(default_factory=list, description="SG1이 고른 옵션", examples=[["OPT-HT", "OPT-IP9K"]])
    assumptions: list[dict[str, Any]] = Field(default_factory=list, description="SG1 가정값 {field, value, ...}")
    contract_risks: list[dict[str, Any]] = Field(
        default_factory=list, description="SG1 계약 리스크 {item, found, limit, ref, suggestion, evidence}"
    )
    demand: dict[str, Any] = Field(
        default_factory=dict,
        description="SG1 물량·일정 {eau, sample_qty, sample_due_date, sop_date, target_price_krw, quote_due_date}",
    )
    supply_analysis: dict[str, Any] = Field(
        description="SG2 결과. TODO: SG2가 state에 넣을 키·형식 합의 전이라 05_expected/5_sg2_supply.json 구조로 받는다"
    )
    issue_date: Optional[str] = Field(default=None, description="발행일 (YYYY-MM-DD). 없으면 오늘")

    # --- SG4·상위 그래프와 주고받는 값 (SupervisorState·SG4State와 같은 이름) ---
    draft_revision: int = Field(
        default=1, ge=1, le=MAX_REVISION,
        description="이번에 저장할 견적 차수. 첫 견적 1, SG4 수정 요청(REVISE) 때 상위 그래프(prepare_rewrite)가 +1. "
        "SG3는 이 값 그대로 save_quote의 revision으로 보내고 돌려준다. 같은 값으로 다시 저장하면 기존 견적 반환",
    )
    reject_reason: Optional[str] = Field(default=None, description="SG4 수정 요청 사유. 있으면 문구 재작성에 반영")
    changes: dict[str, Any] = Field(default_factory=dict, description="SG4 담당자가 요청한 수정 내용. 문구 재작성에 반영")

    # --- SG3 작업 중 값 ---
    quote_draft: dict[str, Any] = Field(default_factory=dict, description="05_expected/6_sg3_quote.json의 quote_draft 구조")
    condition_facts: list[dict[str, Any]] = Field(
        default_factory=list, description="conditions와 같은 순서. 문구에 쓸 수 있는 사실(숫자 검증 기준)"
    )
    messages: list[AnyMessage] = Field(default_factory=list, description="문구 작성 Agent Loop 대화")
    turns: int = Field(default=0, description="모델 호출 횟수")
    tool_calls: dict[str, int] = Field(default_factory=dict, description="도구별 호출 횟수")
    elapsed_sec: float = Field(default=0.0, description="작업 시간 누적 (사람 승인 대기 제외)")
    evidence: list[DocHit] = Field(default_factory=list, description="검색으로 본 근거 조각")
    ready_to_save: bool = Field(default=False, description="문구 검증을 통과해 저장 승인·저장으로 갈 수 있음")
    # SG4의 approval_card(ApprovalCard)와 겹치지 않게 이름을 다르게 둔다
    save_approval_card: Optional[SaveApprovalCard] = None

    # --- SG3가 채우는 값 (SG4State와 같은 이름). draft_revision도 저장한 차수로 돌려준다 ---
    quote_id: Optional[Union[int, str]] = Field(default=None, description="저장한 견적 번호. 수정 요청으로 들어올 때는 직전 견적 번호")
    quote_summary: str = ""
    approval_items: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    sg3_outcome: Optional[Sg3Outcome] = Field(default=None, description="SG3 결과. 상위 그래프의 분기 기준")
    error: Optional[str] = None
