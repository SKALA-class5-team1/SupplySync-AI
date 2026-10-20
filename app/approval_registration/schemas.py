"""
approval_registration 도메인 스키마 정의 (SG4 승인·등록)

SG4 흐름: 승인 안내 준비 -> 승인 요청(멈춤) -> 재고 재확인 -> 견적 등록
승인·반려 기록은 백엔드가 저장하며, SG4는 백엔드가 resume 해 줄 때까지 대기한다.
MCP 도구 입출력 모델은 MCP 도구 명세(business_get_atp 등)를 따른다. 필드 이름은 예시 기준이라
백엔드와 확정되면 이 파일만 맞추면 된다.
"""
from typing import Optional, Union

from pydantic import BaseModel, Field


# ----------------------------------------------------------------------
# MCP 도구 입출력
# ----------------------------------------------------------------------
class AtpPart(BaseModel):
    """business_get_atp 응답의 부품 한 줄."""

    part_id: str = Field(description="부품 코드", examples=["P-MCU-G1"])
    required: int = Field(description="필요 수량", examples=[2000])
    available: int = Field(description="가용 수량 (재고 - 배정)", examples=[1500])
    shortage: int = Field(description="부족 수량 (없으면 0)", examples=[500])
    incoming_qty: Optional[int] = Field(default=None, description="입고 예정 수량")
    incoming_date: Optional[str] = Field(default=None, description="입고 예정일 (YYYY-MM-DD)")
    lead_time_days: Optional[int] = Field(default=None, description="신규 발주 조달 기간 (일)")


class AtpResult(BaseModel):
    """business_get_atp 응답."""

    has_shortage: bool = Field(description="부족한 부품이 하나라도 있는지")
    parts: list[AtpPart] = Field(default_factory=list)


class RegisterResult(BaseModel):
    """business_register_quote 응답."""

    quote_id: Union[int, str]
    status: str = Field(examples=["REGISTERED"])
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
class ExpectedShortage(BaseModel):
    """승인된 계획이 이미 감안한 부족분 (SG2 결과). 재확인 때 비교 기준으로 쓴다."""

    part_id: str = Field(examples=["P-MCU-G1"])
    shortage: int = Field(description="SG2가 계산한 부족 수량", examples=[500])


class ApprovalCard(BaseModel):
    """APPROVAL_REQUIRED 이벤트로 화면에 보내는 승인 카드."""

    quote_id: Union[int, str] = Field(description="SG3가 저장한 견적 번호 (DRAFT)")
    summary: str = Field(default="", examples=["EWP-12-B + OPT-HT + OPT-IP9K, 단가 37,300원"])
    approval_items: list[str] = Field(default_factory=list, description="승인받을 내용 목록")
    evidence: list[DocHit] = Field(default_factory=list, description="근거 문서 조각")


class SG4State(BaseModel):
    """SG4 서브그래프 상태."""

    # --- 앞 단계(SG1~3)가 state에 넣어 둔 값 ---
    thread_id: str = Field(description="LangGraph 실행 ID (재개 키)")
    rfq_id: Union[int, str] = Field(description="RFQ 번호")
    quote_id: Union[int, str] = Field(description="SG3 save_quote(DRAFT)가 돌려준 견적 번호")
    model_id: str = Field(description="선정 모델", examples=["EWP-12-B"])
    options: list[str] = Field(default_factory=list, examples=[["OPT-HT", "OPT-IP9K"]])
    qty: int = Field(description="재확인할 수량 (시작품 수량)", examples=[2000])
    due_date: str = Field(description="고객 납기 (YYYY-MM-DD)", examples=["2026-12-15"])
    expected_shortages: list[ExpectedShortage] = Field(default_factory=list)
    quote_summary: str = ""
    approval_items: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)

    # --- SG4가 채우는 값 ---
    approval_card: Optional[ApprovalCard] = None
    approved: bool = False
    approved_by: Optional[int] = Field(default=None, description="승인자 users.id (resume 본문에서)")
    atp: Optional[AtpResult] = None
    atp_ok: Optional[bool] = None
    registered_quote_id: Optional[Union[int, str]] = None
    registration_status: Optional[str] = None
    error: Optional[str] = None
