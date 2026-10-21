"""백엔드 전용 워크플로 API. 승인자 권한 확인·승인 기록은 백엔드가 담당한다."""

import secrets

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from app.core.config import settings
from app.core.schemas import BaseResponse
from app.supervisor.schemas import JobResult, ResumeJobRequest, StartJobRequest
from app.supervisor.service import WorkflowService, get_service

internal_key = APIKeyHeader(name="X-Internal-Api-Key", auto_error=False)


def require_backend(key: str | None = Security(internal_key)) -> None:
    if not key or not settings.INTERNAL_API_KEY or not secrets.compare_digest(key, settings.INTERNAL_API_KEY):
        raise HTTPException(401, "유효한 내부 API 키가 필요합니다")


router = APIRouter(dependencies=[Depends(require_backend)], responses={
    400: {"model": BaseResponse[None], "description": "요청 형식 오류"},
    401: {"model": BaseResponse[None], "description": "내부 API 키 없음 또는 불일치"},
})


@router.post("/jobs", response_model=BaseResponse[JobResult], summary="SG1~SG4 실행 시작",
             status_code=200, response_description="첫 입력 대기 또는 종료 시 실행 상태",
             responses={409: {"model": BaseResponse[None], "description": "이미 시작한 실행 ID"}})
async def start_job(request: StartJobRequest, service: WorkflowService = Depends(get_service)):
    """첫 담당자 입력 대기 또는 종료까지 실행한다. 동일 thread_id로 중복 시작할 수 없다."""
    return BaseResponse.success(await service.start(request))


@router.get("/jobs/{thread_id}", response_model=BaseResponse[JobResult], summary="실행 상태·승인 요청 조회",
            status_code=200, response_description="현재 공유 상태와 대기 요청",
            responses={404: {"model": BaseResponse[None], "description": "없는 실행 ID"}})
async def get_job(thread_id: str, service: WorkflowService = Depends(get_service)):
    """원 BOM·전체 final_plan·견적 결과와 현재 waiting_for를 반환한다."""
    return BaseResponse.success(await service.get(thread_id))


@router.post("/jobs/{thread_id}/resume", response_model=BaseResponse[JobResult], summary="담당자 확인·승인 후 재개",
             status_code=200, response_description="다음 입력 대기 또는 종료 시 실행 상태",
             responses={
                 404: {"model": BaseResponse[None], "description": "없는 실행 ID"},
                 409: {"model": BaseResponse[None], "description": "현재 대기 ID·단계 불일치 또는 종료된 실행"},
                 422: {"model": BaseResponse[None], "description": "담당자 확인·승인 본문 오류"},
             })
async def resume_job(thread_id: str, request: ResumeJobRequest, service: WorkflowService = Depends(get_service)):
    """백엔드에서 승인자 검증·기록을 마친 응답을 현재 interrupt_id와 함께 전달한다."""
    return BaseResponse.success(await service.resume(thread_id, request))
