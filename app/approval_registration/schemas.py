"""
approval_registration 도메인 스키마 정의 (SG4 승인·등록)

SG4 흐름: 승인 안내 준비 -> 승인 요청(멈춤) -> 재고 재확인 -> 견적 등록
승인·반려 기록은 백엔드가 저장하며, SG4는 백엔드가 resume 해 줄 때까지 대기한다.
MCP 도구 입출력 모델은 business-mcp 레포의 CLAUDE.md(check_parts_supply, register_quote)를 따른다.
"""
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field


# ----------------------------------------------------------------------
# MCP 도구 입출력
# ----------------------------------------------------------------------
class SupplyPart(BaseModel):
    """check_parts_supply 응답의 부품 한 줄. (그 밖의 필드는 쓰지 않아 무시한다)"""

    part_id: str = Field(description="부품 코드", examples=["P-MCU-G1"])
    part_name: Optional[str] = None
    required_qty: int = Field(description="필요 수량", examples=[1500])
    available: int = Field(description="가용 수량 (재고 - 배정)", examples=[1500])
    short_qty: int = Field(description="부족 수량 (없으면 0)", examples=[0])
    status: str = Field(description="OK 또는 SHORT", examples=["OK"])
    incoming_qty: Optional[int] = Field(default=None, description="입고 예정 수량")
    incoming_date: Optional[str] = Field(default=None, description="입고 예정일 (YYYY-MM-DD)")


class SupplyResult(BaseModel):
    """check_parts_supply 응답."""

    due_date: Optional[str] = None
    base_date: Optional[str] = None
    has_risk: bool = False
    parts: list[SupplyPart] = Field(default_factory=list)


class RegisterResult(BaseModel):
    """register_quote 응답."""

    quote_id: Union[int, str]
    status: str = Field(examples=["REGISTERED"])
    rfq_status: Optional[str] = Field(default=None, examples=["QUOTED"])
    registered_at: Optional[str] = None


class DocHit(BaseModel):
    """rag_search_docs 응답의 문서 조각 한 건."""

    chunk_id: str = Field(examples=["pol-003"])
    section: str = Field(default="", examples=["제12조 품질 보증 기간"])
    content: str = ""
    score: Optional[float] = None


# ----------------------------------------------------------------------
# 그래프 상태
# ----------------------------------------------------------------------
class ApprovalCard(BaseModel):
    """APPROVAL_REQUIRED 이벤트로 화면에 보내는 승인 카드."""

    quote_id: Union[int, str] = Field(description="SG3가 저장한 견적 번호 (DRAFT)")
    summary: str = Field(default="", examples=["EWP-12-B + OPT-HT + OPT-IP9K, 단가 37,300원"])
    approval_items: list[str] = Field(default_factory=list, description="승인받을 내용 목록")
    evidence: list[DocHit] = Field(default_factory=list, description="근거 문서 조각")


# SG4 가 상위 그래프에 돌려주는 결과
#   REGISTERED            견적 등록 완료
#   REWRITE_REQUIRED      반려됨 -> 상위 그래프가 SG3 로 돌려 보낸다
#   STOCK_REVIEW_REQUIRED 승인 뒤 재고가 부족해 등록 차단. 다음 단계(SG2 복귀 등)는 팀 합의 필요
#   INVALID_APPROVAL      승인 입력이 계속 잘못 와서 중단
Sg4Outcome = Literal["REGISTERED", "REWRITE_REQUIRED", "STOCK_REVIEW_REQUIRED", "INVALID_APPROVAL"]


class SG4State(BaseModel):
    """SG4 서브그래프 상태."""

    # --- 앞 단계(SG1~3)가 state에 넣어 둔 값 ---
    thread_id: str = Field(description="LangGraph 실행 ID (재개 키)")
    rfq_id: Union[int, str] = Field(description="RFQ 번호")
    quote_id: Union[int, str] = Field(description="SG3 save_quote(DRAFT)가 돌려준 견적 번호")
    final_plan: dict[str, int] = Field(
        description="SG2가 정한 최종 조달 계획 (부품 코드 -> 수량). 승인 후 이 수량으로 재고를 다시 확인한다",
        examples=[{"P-MCU-G1": 1500, "P-MCU-G1-ALT": 500}],
    )
    due_date: str = Field(description="고객 납기 (YYYY-MM-DD)", examples=["2026-12-15"])
    base_date: Optional[str] = Field(default=None, description="검토 기준일 (없으면 서버가 오늘로 계산)")
    quote_summary: str = ""
    approval_items: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    # --- SG4가 채우는 값 ---
    approval_card: Optional[ApprovalCard] = None
    approved: bool = False
    rejected: bool = False
    reject_reason: Optional[str] = Field(default=None, description="반려 사유 (상위 그래프가 SG3 재작성에 쓴다)")
    supply: Optional[SupplyResult] = None
    recheck_ok: Optional[bool] = None
    sg4_outcome: Optional[Sg4Outcome] = Field(default=None, description="SG4 결과. 상위 그래프의 분기 기준")
    registered_quote_id: Optional[Union[int, str]] = None
    registration_status: Optional[str] = None
    error: Optional[str] = None
