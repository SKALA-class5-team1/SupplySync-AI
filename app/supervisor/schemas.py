"""백엔드가 호출하는 워크플로 시작·조회·재개 API."""

from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class StartJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1, description="백엔드가 발급한 실행 ID. 모든 SG에서 동일하게 유지")
    rfq_number: str = Field(min_length=1, description="검토할 RFQ 번호")
    base_date: date | None = Field(default=None, description="재현용 공급 기준일. 생략하면 매 조회 시 서버의 오늘")
    issue_date: date | None = Field(default=None, description="견적 발행일. 생략하면 SG3 실행일")


class ResumeJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1, description="현재 응답 waiting_for.id. 이전 승인 응답 재사용 방지")
    response: dict[str, Any] = Field(description="대기 step에 해당하는 SG1 확인 또는 SG3·SG4 승인 본문")


class PendingInput(BaseModel):
    id: str
    payload: dict[str, Any]


class JobResult(BaseModel):
    thread_id: str
    status: str
    current_stage: str
    next_step: str
    waiting_for: PendingInput | None = None
    state: dict[str, Any] = Field(description="SG1~SG4 공유 상태. bom과 전체 final_plan을 모두 포함")
