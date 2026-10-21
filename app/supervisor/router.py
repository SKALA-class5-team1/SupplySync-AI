"""백엔드 전용 워크플로 API. 승인자 권한 확인·승인 기록은 백엔드가 담당한다."""

import secrets

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader

from app.core.config import settings
from app.core.schemas import BaseResponse
from app.supervisor.schemas import ResumeRunRequest, RunAcceptance, RunResult, StartRunRequest
from app.supervisor.service import WorkflowService, get_service

internal_key = APIKeyHeader(name="X-Internal-Api-Key", auto_error=False)


def require_backend(key: str | None = Security(internal_key)) -> None:
    if not key or not settings.INTERNAL_API_KEY or not secrets.compare_digest(key, settings.INTERNAL_API_KEY):
        raise HTTPException(401, "유효한 내부 API 키가 필요합니다")


router = APIRouter(dependencies=[Depends(require_backend)], responses={
    400: {"model": BaseResponse[None], "description": "요청 형식 오류"},
    401: {"model": BaseResponse[None], "description": "내부 API 키 없음 또는 불일치"},
    503: {"model": BaseResponse[None], "description": "서버 종료 중"},
})


@router.post("/runs", response_model=BaseResponse[RunAcceptance], summary="SG1~SG4 실행 접수",
             status_code=202, response_description="접수 결과. 진행·실패·완료는 BE 이벤트로 전송",
             responses={
                 409: {"model": BaseResponse[None], "description": "동일 식별자에 다른 실행 요청"},
             })
async def start_run(request: StartRunRequest, service: WorkflowService = Depends(get_service)):
    """즉시 접수하고 백그라운드에서 실행한다. 같은 request_id는 기존 접수 결과를 반환한다."""
    return BaseResponse.success(await service.start(request))


@router.get("/runs/{thread_id}", response_model=BaseResponse[RunResult], summary="실행 상태·승인 요청 조회",
            status_code=200, response_description="현재 공유 상태와 대기 요청",
            responses={404: {"model": BaseResponse[None], "description": "없는 실행 ID"}})
async def get_run(thread_id: str, service: WorkflowService = Depends(get_service)):
    """원 BOM·전체 final_plan·견적 결과와 현재 waiting_for를 반환한다."""
    return BaseResponse.success(await service.get(thread_id))


@router.post("/runs/{thread_id}/resume", response_model=BaseResponse[RunAcceptance], summary="담당자 확인·승인 재개 접수",
             status_code=202, response_description="재개 접수 결과. 그래프는 백그라운드에서 실행",
             responses={
                 404: {"model": BaseResponse[None], "description": "없는 실행 ID"},
                 409: {"model": BaseResponse[None], "description": "현재 대기 ID·단계 불일치 또는 종료된 실행"},
                 422: {"model": BaseResponse[None], "description": "담당자 확인·승인 본문 오류"},
             })
async def resume_run(thread_id: str, request: ResumeRunRequest, service: WorkflowService = Depends(get_service)):
    """BE의 kind/step·userId·decision을 현재 체크포인트의 interrupt 응답으로 변환한다."""
    return BaseResponse.success(await service.resume(thread_id, request))
