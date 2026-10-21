"""
SG1 (RFQ·제품 검토) 서브그래프 state와 그래프 내부 데이터 모델.

API 요청·응답 모델은 schemas.py에 둡니다. 이 파일은 그래프 안에서만 쓰입니다.
state에는 체크포인터 저장을 위해 dict/list로 변환한 값만 넣습니다.
"""
from typing import Literal, Optional, TypedDict

from pydantic import BaseModel, Field

IssueType = Literal["OUTLIER", "MISSING", "CONTRACT_RISK"]
Severity = Literal["HIGH", "MED", "LOW"]
ReviewStatus = Literal["WAITING", "DONE"]

ISSUE_LABELS: dict[str, str] = {
    "OUTLIER": "이상값",
    "MISSING": "누락",
    "CONTRACT_RISK": "계약 리스크",
}


# ---------------------------------------------------------------------------
# parse_rfq 추출 양식 (data/rfq_dataset/01_rfq/rfq_schema.json)
# ---------------------------------------------------------------------------
class Volume(BaseModel):
    eau: Optional[int] = Field(None, description="연간 예상 물량 (EAU)")
    supply_years: Optional[int] = Field(None, description="공급 기간 (년)")
    lifetime_volume: Optional[int] = Field(None, description="총 공급 물량")
    sample_qty: Optional[int] = Field(None, description="시작품(샘플) 수량")
    sample_due_date: Optional[str] = Field(None, description="시작품 납품일 YYYY-MM-DD")


class Schedule(BaseModel):
    sop_date: Optional[str] = Field(None, description="양산 시작일(SOP) YYYY-MM-DD")
    ppap_date: Optional[str] = Field(None, description="양산 승인(PPAP) 완료일 YYYY-MM-DD")


class Flow(BaseModel):
    value: Optional[float] = Field(None, description="유량 숫자")
    unit: Optional[str] = Field(None, description="원문 단위 그대로 (L/min, L/h ...)")


class MaxSize(BaseModel):
    l: Optional[float] = Field(None, description="최대 길이 mm")
    w: Optional[float] = Field(None, description="최대 폭 mm")
    h: Optional[float] = Field(None, description="최대 높이 mm")


class Spec(BaseModel):
    flow: Optional[Flow] = Field(None, description="유량 (단위 변환 금지)")
    head_kpa: Optional[float] = Field(None, description="유량 기준 수두 kPa")
    voltage_min: Optional[float] = None
    voltage_max: Optional[float] = None
    temp_min: Optional[float] = None
    temp_max: Optional[float] = None
    ip_rating: Optional[str] = Field(None, description="방수 등급, 예: IP6K9K")
    noise_db_max: Optional[float] = None
    comm: Optional[str] = Field(None, description="통신 방식 이름만, 예: LIN, PWM")
    max_size_mm: Optional[MaxSize] = None
    max_weight_kg: Optional[float] = None
    connector: Optional[str] = Field(None, description="커넥터 사양. TBD/미기재면 null")
    coolant: Optional[str] = Field(None, description="냉각수 종류. 미기재면 null")


class Quality(BaseModel):
    certifications: list[str] = Field(default_factory=list)
    ppap_level: Optional[int] = None


class Delivery(BaseModel):
    plant: Optional[str] = None
    incoterms: Optional[str] = None
    packing: Optional[str] = None


class Commercial(BaseModel):
    target_price_krw: Optional[int] = None
    annual_cr_pct: Optional[float] = Field(None, description="연간 가격 인하율 %")
    payment_days: Optional[int] = Field(None, description="결제 기간 일수 (익월 60일 → 60)")
    payment_terms: Optional[str] = Field(None, description="결제 조건 원문")
    currency: Optional[str] = None


class Contract(BaseModel):
    warranty_years: Optional[float] = None
    warranty_km: Optional[int] = None
    penalty_cap_pct: Optional[float] = Field(None, description="지연 배상 상한 %. 상한 없음이면 null")
    eol_notice_months: Optional[int] = None


class ParsedRFQ(BaseModel):
    """RFQ 원문에서 추출한 구조화 데이터. 원문에 없거나 TBD면 null."""

    rfq_id: Optional[str] = None
    customer: Optional[str] = None
    project: Optional[str] = None
    item: Optional[str] = None
    issue_date: Optional[str] = Field(None, description="발행일 YYYY-MM-DD")
    quote_due_date: Optional[str] = Field(None, description="견적 회신 마감일 YYYY-MM-DD")
    volume: Volume = Field(default_factory=Volume)
    schedule: Schedule = Field(default_factory=Schedule)
    spec: Spec = Field(default_factory=Spec)
    quality: Quality = Field(default_factory=Quality)
    delivery: Delivery = Field(default_factory=Delivery)
    commercial: Commercial = Field(default_factory=Commercial)
    contract: Contract = Field(default_factory=Contract)


# ---------------------------------------------------------------------------
# 검증·매칭 결과
# ---------------------------------------------------------------------------
class Evidence(BaseModel):
    chunk_id: str
    section: str
    excerpt: str


class Issue(BaseModel):
    id: str
    type: IssueType
    label: str
    field: str
    severity: Severity
    found: str
    reason: Optional[str] = None
    suggestion: Optional[str] = None
    ref: Optional[str] = None
    evidence: list[Evidence] = Field(default_factory=list)
    # 담당자에게 제안할 값 (단위 오기 추정값, 누락 항목의 기본 가정값)
    proposed_value: Optional[str] = None
    # 계약 리스크의 사내 한도 표기
    limit: Optional[str] = None
    # 담당자가 승인한 값으로 해결되었는지
    resolved: bool = False


class Assumption(BaseModel):
    field: str
    value: str
    issue_id: Optional[str] = None
    approved_by: Optional[int] = Field(None, description="승인한 user_id. 미승인 기본 가정이면 null")


class ContractRisk(BaseModel):
    item: str
    found: str
    limit: Optional[str] = None
    ref: Optional[str] = None
    suggestion: Optional[str] = None
    evidence: list[Evidence] = Field(default_factory=list)


class CustomerQuestions(BaseModel):
    """build_report LLM 출력: 이슈 하나당 질문 하나."""

    questions: list[str] = Field(description="고객에게 보낼 확인 질문. 입력 이슈 순서대로 한 문장씩")


# ---------------------------------------------------------------------------
# 그래프 state
# ---------------------------------------------------------------------------
class SG1State(TypedDict, total=False):
    # 입력 (thread_id는 백엔드가 발급)
    rfq_number: str
    thread_id: str

    # 중간 값
    rfq_content: dict
    confirmed_at: Optional[str]

    # 출력 (SG2로 전달)
    review_id: Optional[int]
    parsed_rfq: dict
    normalized_spec: dict
    issues: list[dict]
    passed_checks: list[str]
    assumptions: list[dict]
    confirmed_by: Optional[int]
    selected_model: Optional[str]
    options: list[str]
    fit_type: Optional[str]
    match_result: dict
    contract_risks: list[dict]
    customer_questions: list[str]
    demand: dict
