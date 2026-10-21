"""백엔드의 실행·재개 계약. 내부 interrupt ID는 API 계층에서 찾는다."""

from datetime import date
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Identifier = Annotated[str, Field(min_length=1)]
UserId = Annotated[int, Field(strict=True, gt=0)]


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: Identifier
    rfq_id: Annotated[int, Field(strict=True, gt=0)] | Identifier
    rfq_number: Identifier
    thread_id: Identifier
    request_id: Identifier
    base_date: date | None = Field(default=None, description="재현용 공급 기준일")
    issue_date: date | None = Field(default=None, description="재현용 견적 발행일")


class AssumptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: Identifier
    value: Identifier


class ClarificationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assumptions: list[AssumptionInput] = Field(min_length=1)


class ClarificationResume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: Identifier
    thread_id: Identifier
    kind: Literal["CLARIFICATION"]
    userId: UserId
    decision: ClarificationDecision


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: Literal["APPROVED", "REJECTED", "REVISE"]
    reason: str | None = None
    changes: dict[str, Any] = Field(default_factory=dict)


class ApprovalResume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: Literal["SG4.request_approval"]
    userId: UserId
    decision: ApprovalDecision
    quoteId: int | str | None = Field(default=None, description="전달되면 현재 견적과 대조")


ResumeRunRequest = ClarificationResume | ApprovalResume


class PendingInput(BaseModel):
    id: str
    payload: dict[str, Any]


class RunResult(BaseModel):
    task_id: str
    request_id: str
    thread_id: str
    status: str
    current_stage: str
    next_step: str
    waiting_for: PendingInput | None = None
    state: dict[str, Any] = Field(description="원 BOM과 전체 final_plan을 포함한 공유 상태")
