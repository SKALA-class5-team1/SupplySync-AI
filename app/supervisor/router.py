"""백엔드 전용 워크플로 API. 승인자 권한 확인·승인 기록은 백엔드가 담당한다."""

import secrets

from fastapi import APIRouter, Depends, HTTPException, Response, Security
from fastapi.security import APIKeyHeader

from app.core.config import settings
from app.core.schemas import BaseResponse
from app.supervisor.schemas import ResumeRunRequest, RunResult, StartRunRequest
from app.supervisor.service import WorkflowService, get_service

internal_key = APIKeyHeader(name="X-Internal-Api-Key", auto_error=False)


def require_backend(key: str | None = Security(internal_key)) -> None:
    if not key or not settings.INTERNAL_API_KEY or not secrets.compare_digest(key, settings.INTERNAL_API_KEY):
        raise HTTPException(401, "유효한 내부 API 키가 필요합니다")


router = APIRouter(dependencies=[Depends(require_backend)], responses={
    400: {"model": BaseResponse[None], "description": "요청 형식 오류"},
    401: {"model": BaseResponse[None], "description": "내부 API 키 없음 또는 불일치"},
    502: {"model": BaseResponse[None], "description": "실행·도구·이벤트 전송 실패. metadata.exception에 원인"},
    504: {"model": BaseResponse[None], "description": "실행 제한 시간 초과"},
})


@router.post("/runs", response_model=BaseResponse[RunResult], summary="SG1~SG4 실행 시작",
             status_code=200, response_description="첫 입력 대기 또는 종료 시 실행 상태",
             responses={
                 202: {"model": BaseResponse[RunResult], "description": "실행 중인 동일 request_id의 기존 접수 결과"},
                 409: {"model": BaseResponse[None], "description": "동일 식별자에 다른 실행 요청"},
             })
async def start_run(request: StartRunRequest, response: Response, service: WorkflowService = Depends(get_service)):
    """입력 대기 또는 종료까지 실행한다. 같은 request_id의 재요청은 기존 접수 결과를 반환한다."""
    result = await service.start(request)
    if result.status == "ACCEPTED":
        response.status_code = 202
    return BaseResponse.success(result)


@router.get("/runs/{thread_id}", response_model=BaseResponse[RunResult], summary="실행 상태·승인 요청 조회",
            status_code=200, response_description="현재 공유 상태와 대기 요청",
            responses={404: {"model": BaseResponse[None], "description": "없는 실행 ID"}})
async def get_run(thread_id: str, service: WorkflowService = Depends(get_service)):
    """원 BOM·전체 final_plan·견적 결과와 현재 waiting_for를 반환한다."""
    return BaseResponse.success(await service.get(thread_id))


@router.post("/runs/{thread_id}/resume", response_model=BaseResponse[RunResult], summary="담당자 확인·승인 후 재개",
             status_code=200, response_description="다음 입력 대기 또는 종료 시 실행 상태",
             responses={
                 404: {"model": BaseResponse[None], "description": "없는 실행 ID"},
                 409: {"model": BaseResponse[None], "description": "현재 대기 ID·단계 불일치 또는 종료된 실행"},
                 422: {"model": BaseResponse[None], "description": "담당자 확인·승인 본문 오류"},
             })
async def resume_run(thread_id: str, request: ResumeRunRequest, service: WorkflowService = Depends(get_service)):
    """BE의 kind/step·userId·decision을 현재 체크포인트의 interrupt 응답으로 변환한다."""
    return BaseResponse.success(await service.resume(thread_id, request))
