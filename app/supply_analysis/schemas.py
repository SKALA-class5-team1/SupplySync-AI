"""SG2 입력, MCP 응답, 다음 단계에 전달하는 공급 계획."""

from datetime import date
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

Quantity = Annotated[int, Field(strict=True, ge=0)]
PositiveQuantity = Annotated[int, Field(strict=True, gt=0)]
AlternativeType = Literal[
    "WAIT_INCOMING", "PARTIAL_DELIVERY", "SUBSTITUTE_PART", "NEW_ORDER"
]
Criterion = Literal["meets_due_date", "extra_cost_krw", "validation_days", "quality_risk"]


class Demand(BaseModel):
    sample_qty: PositiveQuantity = Field(description="시작품 수량", examples=[2000])
    sample_due_date: date = Field(description="시작품 납기", examples=["2026-12-15"])


class SupplyAnalysisRequest(BaseModel):
    """가이드의 SG1 출력. 공통 State 입력은 service에서 이 형식으로 정규화한다."""

    thread_id: str = Field(min_length=1, description="SG1과 같은 실행 ID", examples=["th_sg2_demo"])
    rfq_number: str = Field(min_length=1, description="RFQ 번호", examples=["HB-EV7-EWP-001"])
    review_id: int | str | None = Field(default=None, description="SG1 검토 ID", examples=[1])
    selected_model: str = Field(min_length=1, description="SG1이 선택한 모델", examples=["EWP-12-B"])
    options: list[str] = Field(default_factory=list, description="SG1이 선택한 옵션", examples=[["OPT-HT", "OPT-IP9K"]])
    demand: Demand = Field(description="시작품 수량과 납기")
    contract_risks: list[dict[str, Any]] = Field(default_factory=list, description="SG1 계약 위험", examples=[[{"field": "contract.penalty_cap_pct", "item": "지연 배상 상한 없음"}]])
    base_date: date | None = Field(default=None, description="기준일. 생략하면 MCP가 서울 기준 오늘을 사용", examples=["2026-10-19"])
    feedback: dict[str, Any] | str | None = Field(default=None, description="상위 그래프의 재검토 사유", examples=["재고 변동으로 공급 계획 재검토"])


class BomPart(BaseModel):
    model_config = ConfigDict(extra="allow")

    part_id: str
    part_name: str
    qty_per_unit: PositiveQuantity
    required_qty: PositiveQuantity
    source: str
    replaced_part_id: str | None = None


class BomResult(BaseModel):
    model_id: str
    options: list[str] = Field(default_factory=list)
    qty: PositiveQuantity
    parts: list[BomPart] = Field(min_length=1)


class SupplyPart(BaseModel):
    model_config = ConfigDict(extra="allow")

    part_id: str
    part_name: str = ""
    required_qty: PositiveQuantity
    available: Quantity
    short_qty: Quantity
    status: Literal["OK", "SHORT"]
    incoming_qty: Quantity | None = None
    incoming_date: date | None = None
    incoming_before_due: bool | None = None
    incoming_delay_days: Quantity | None = None
    new_order_arrival: date | None = None
    lead_time_days: Quantity | None = None

    @model_validator(mode="after")
    def validate_shortage(self):
        if self.short_qty != max(0, self.required_qty - self.available):
            raise ValueError(f"{self.part_id}: 가용 수량과 부족 수량이 일치하지 않습니다")
        if (self.status == "SHORT") != (self.short_qty > 0):
            raise ValueError(f"{self.part_id}: 재고 상태와 부족 수량이 일치하지 않습니다")
        return self


class SupplyResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    due_date: date
    base_date: date
    has_risk: bool = False
    parts: list[SupplyPart] = Field(min_length=1)


class Substitute(BaseModel):
    substitute_part_id: str
    part_name: str
    supplier: str
    available: Quantity
    covers_shortage: bool | None = None
    unit_cost_krw: Quantity
    unit_cost_diff_krw: Annotated[int, Field(strict=True)]
    validation_days: Quantity
    usable_date: date
    lead_time_days: Quantity
    pin_compatible: bool
    ref_chunk_id: str


class SubstitutesResult(BaseModel):
    part_id: str
    part_name: str
    unit_cost_krw: Quantity
    substitutes: list[Substitute]


class DocHit(BaseModel):
    chunk_id: str
    section: str
    content: str
    score: float | None = None


class Evidence(BaseModel):
    chunk_id: str = Field(description="RAG 청크 ID", examples=["alt-003"])
    section: str = Field(description="문서 절 제목", examples=["3. 대안 비교 기준"])
    excerpt: str = Field(description="조회된 원문 인용", examples=["대안은 납기 충족 여부, 추가 원가, 검증 기간, 품질 위험 순으로 비교한다."])


class Shortage(BaseModel):
    """팀 공통 State의 부족 부품 형식. 원본 MCP 값은 supply_check에 보존한다."""

    part: str = Field(description="부품 ID", examples=["P-MCU-G1"])
    needed: PositiveQuantity = Field(description="필요 수량", examples=[2000])
    available: Quantity = Field(description="가용 수량", examples=[1500])
    short_qty: PositiveQuantity = Field(description="부족 수량", examples=[500])
    eta_days: Quantity | None = Field(description="기준일부터 예정 입고일까지의 일수", examples=[93])


class Alternative(BaseModel):
    part_id: str = Field(description="부족한 원 부품", examples=["P-MCU-G1"])
    type: AlternativeType = Field(description="고정 대안 4종", examples=["SUBSTITUTE_PART"])
    feasible: bool = Field(description="수량·호환성·계약 조건상 실행 가능 여부", examples=[True])
    meets_due_date: bool = Field(description="전량 납기 충족 여부", examples=[True])
    delay_days: Quantity | None = Field(description="전량 확보 지연일. 일정 불명이면 null", examples=[0])
    extra_cost_krw: int = Field(description="부족 수량에 대한 추가 원가", examples=[150000])
    validation_days: Quantity = Field(description="검증 기간", examples=[14])
    quality_risk: Literal["LOW", "VALIDATION_REQUIRED", "UNKNOWN"] = Field(description="기존 부품/검증 필요/확인 불가", examples=["VALIDATION_REQUIRED"])
    on_time_qty: Quantity = Field(description="정시 확보 수량", examples=[2000])
    delayed_qty: Quantity = Field(description="지연 또는 일정 미확정 수량", examples=[0])
    substitute_part_id: str | None = Field(default=None, description="선택한 대체 부품", examples=["P-MCU-G1-ALT"])
    usable_date: date | None = Field(default=None, description="부족분 사용 가능일", examples=["2026-11-02"])
    reason: str = Field(description="계산·문서에 근거한 설명", examples=["검증 완료 후 납기 내 500개 공급 가능"])
    constraints: list[str] = Field(default_factory=list, description="실행 조건 또는 불가 사유", examples=[["검증 시험 통과 후 생산 투입", "추가 비용은 견적 조건에 별도 표시"]])
    evidence: list[Evidence] = Field(default_factory=list, description="RAG 근거")


class SelectedAlternative(BaseModel):
    type: AlternativeType
    parts: list[Alternative]
    meets_due_date: bool
    extra_cost_krw: int
    validation_days: Quantity
    quality_risk: Literal["LOW", "VALIDATION_REQUIRED", "UNKNOWN"]
    final_plan: dict[str, PositiveQuantity] = Field(description="대안을 적용한 전체 최종 부품 ID와 필요 수량")
    reason: str


class SupplyAnalysisResult(BaseModel):
    bom: list[BomPart] = Field(default_factory=list, description="공통 State BOM")
    bom_effective: list[BomPart] = Field(default_factory=list, description="옵션 반영 원 BOM. 최종 부품 ID·수량은 final_plan 사용")
    supply_check: SupplyResult | None = Field(default=None, description="MCP 원본 재고 확인 결과")
    shortages: list[Shortage] = Field(default_factory=list, description="공통 형식 부족 부품")
    alternatives: list[Alternative] = Field(default_factory=list, description="부족 부품마다 4종의 대안")
    selected_alternative: SelectedAlternative | None = Field(default=None, description="전체 부족분에 대해 선택한 계획")
    recommended_alt: AlternativeType | None = Field(default=None, description="DB 저장용 추천 대안")
    final_plan: dict[str, PositiveQuantity] = Field(
        default_factory=dict,
        description="전체 최종 부품 ID → 필요 수량. 변경 없는 부품도 포함하며 전량 대체된 원부품은 제외. 부족이 없으면 원 BOM 전체, 보류·실패 시 {}",
        examples=[{
            "P-CON-4P": 2000, "P-HSG-AL": 2000, "P-IMP-25": 2000,
            "P-MCU-G1": 1500, "P-MCU-G1-ALT": 500, "P-MTR-BL12": 2000, "P-SEAL-9K": 2000,
        }],
    )
    review_id: int | str | None = Field(default=None, description="저장한 검토 ID", examples=[1])
    saved: bool = Field(default=False, description="SG2 결과 저장 성공 여부", examples=[True])
    status: Literal["DRAFTING", "ON_HOLD"] = Field(default="ON_HOLD", description="다음 작업 상태", examples=["DRAFTING"])
    next_step: Literal["quote_drafting", "hold"] = Field(default="hold", description="상위 그래프 이동 경로", examples=["quote_drafting"])
    error: str | None = Field(default=None, description="보류 원인", examples=[None])


class SG2State(TypedDict, total=False):
    """공통 State 키 + SG2 내부 계산 값. 반환은 service에서 출력 키만 추린다."""

    thread_id: str
    rfq_number: str
    review_id: int | str | None
    selected_model: str
    options: list[str]
    demand: dict[str, Any]
    contract_risks: list[dict[str, Any]]
    base_date: str | None
    requirements: dict[str, Any]
    matched_product: dict[str, Any]
    feedback: dict[str, Any] | str | None
    request: SupplyAnalysisRequest
    bom: list[dict[str, Any]]
    bom_effective: list[dict[str, Any]]
    supply_check: dict[str, Any] | None
    shortages: list[dict[str, Any]]
    alternatives: list[dict[str, Any]]
    selected_alternative: dict[str, Any] | None
    recommended_alt: AlternativeType | None
    final_plan: dict[str, int]
    saved: bool
    status: str
    next_step: str
    error: str | None
    candidates: dict[str, list[Alternative]]
    criteria: list[Criterion]
    substitutes: dict[str, SubstitutesResult]


