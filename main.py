import uvicorn
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.approval_registration.router import router as approval_registration_router
from app.core.config import settings
from app.core.logging_config import setup_logging
from app.core.observability import setup_tracing
from app.core.middleware import (
    RequestIDMiddleware,
    LoggingMiddleware,
    SecurityHeadersMiddleware
)
from app.core.exceptions import (
    CustomException,
    custom_exception_handler,
    validation_exception_handler,
    http_exception_handler,
    starlette_http_exception_handler,
    general_exception_handler
)
from app.core.schemas import HealthCheckResponse, ServiceInfoResponse
from app.quote_drafting.router import router as quote_drafting_router
from app.rfq_review.router import router as rfq_review_router
from app.supply_analysis.router import router as supply_analysis_router
from app.supervisor.router import router as supervisor_router

setup_logging()
setup_tracing()

app = FastAPI(
    title=settings.APP_NAME,
    description="SupplySync AI의 RFQ 검토, 공급 분석, 견적 작성 및 승인 등록 API입니다.",
    version=settings.APP_VERSION,
    debug=settings.DEBUG,
    docs_url="/docs",
    redoc_url=f"{settings.API_V1_PREFIX}/redoc",
    openapi_url=f"{settings.API_V1_PREFIX}/openapi.json",
    openapi_tags=[
        {
            "name": "System",
            "description": "서비스 기본 정보와 애플리케이션 생존 상태를 확인하는 공개 API입니다.",
        },
    ],
    swagger_ui_parameters={
        "filter": True,
        "displayRequestDuration": True,
    },
)

# 미들웨어는 역순으로 실행되므로 요청 ID를 로깅 전에 생성합니다.
app.add_middleware(LoggingMiddleware)
app.add_middleware(RequestIDMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
if settings.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# 공통 오류 응답
app.add_exception_handler(CustomException, custom_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(StarletteHTTPException, starlette_http_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

# SupplySync 도메인 라우터
app.include_router(supervisor_router, prefix=settings.API_V1_PREFIX, tags=["Workflow"])
for router, path, tag in (
    (rfq_review_router, "rfq-review", "RFQ Review"),
    (supply_analysis_router, "supply-analysis", "Supply Analysis"),
    (quote_drafting_router, "quote-drafting", "Quote Drafting"),
    (approval_registration_router, "approval-registration", "Approval Registration"),
):
    app.include_router(router, prefix=f"{settings.API_V1_PREFIX}/{path}", tags=[tag])


@app.get(f"{settings.API_V1_PREFIX}/docs", include_in_schema=False)
def docs_redirect(request: Request):
    return RedirectResponse(url=request.url_for("swagger_ui_html"))


# 문서 작성 예시: 데코레이터에는 API 명세, docstring에는 사용 설명,
# 응답 모델(app/core/schemas.py)에는 필드 설명과 예시를 정의합니다.
@app.get(
    "/",
    tags=["System"],
    summary="서비스 기본 정보 조회",
    response_model=ServiceInfoResponse,
    status_code=status.HTTP_200_OK,
    response_description="서비스 이름, 실행 환경, API 버전을 반환합니다.",
)
def root() -> ServiceInfoResponse:
    """
    현재 실행 중인 SupplySync AI의 기본 정보를 조회합니다.

    - **사용 목적**: 접속한 서비스와 배포 환경, API 버전을 확인합니다.
    - **인증**: 필요하지 않습니다.
    - **요청 파라미터 및 본문**: 없습니다.
    - **성공 응답**: `200 OK`. `message`는 서비스 이름,
      `environment`는 실행 환경, `version`은 API 버전입니다.

    응답 예시의 값은 로컬 환경 기준이며 실제 값은 서버 설정에 따라 달라집니다.
    """
    return ServiceInfoResponse(
        message=settings.APP_NAME,
        environment=settings.ENVIRONMENT,
        version=settings.APP_VERSION,
    )


@app.get(
    "/health",
    tags=["System"],
    summary="애플리케이션 생존 상태 확인",
    response_model=HealthCheckResponse,
    status_code=status.HTTP_200_OK,
    response_description="요청에 응답 가능한 상태(healthy)와 실행 환경을 반환합니다.",
)
def health_check() -> HealthCheckResponse:
    """
    애플리케이션이 HTTP 요청에 응답할 수 있는지 확인하는 생존 검사(liveness)입니다.

    - **사용 목적**: 모니터링 도구나 컨테이너의 생존 검사에 사용합니다.
    - **인증**: 필요하지 않습니다.
    - **요청 파라미터 및 본문**: 없습니다.
    - **성공 응답**: `200 OK`. `status`는 `healthy`,
      `environment`는 현재 실행 환경입니다.

    MCP 서버, 외부 API, AI 모델의 연결 상태는 검사하지 않으므로,
    이 응답만으로 전체 업무 기능의 준비 상태(readiness)를 판단할 수 없습니다.
    서버가 응답할 수 없는 경우에는 이 API의 JSON 응답 자체를 받지 못할 수 있습니다.
    """
    return HealthCheckResponse(
        status="healthy",
        environment=settings.ENVIRONMENT,
    )


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.RELOAD,
        log_level=settings.LOG_LEVEL.lower()
    )
