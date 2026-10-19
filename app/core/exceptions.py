"""
커스텀 예외 및 예외 핸들러
"""
from typing import Optional
from fastapi import Request, status
from fastapi.exceptions import RequestValidationError, HTTPException
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from app.core.error_code import ErrorCode
from app.core.schemas import BaseResponse


class CustomException(Exception):
    """
    커스텀 예외 클래스
    
    ErrorCode와 상세 메시지를 받아 처리합니다.
    """
    def __init__(
        self,
        error_code: ErrorCode,
        detail_message: Optional[str] = None
    ):
        self.error_code = error_code
        self.detail_message = detail_message or error_code.description
        super().__init__(self.detail_message)


async def custom_exception_handler(
    request: Request,
    exc: CustomException
) -> JSONResponse:
    """CustomException 핸들러"""
    response = BaseResponse.failure(
        error_code=exc.error_code,
        detail_message=exc.detail_message
    )
    
    return JSONResponse(
        status_code=exc.error_code.http_status.value,
        content=response.model_dump(by_alias=True)
    )


async def validation_exception_handler(
    request: Request,
    exc: RequestValidationError
) -> JSONResponse:
    """RequestValidationError 핸들러 (Pydantic 검증 오류)"""
    errors = exc.errors()
    error_messages = []
    
    for error in errors:
        field = ".".join(str(loc) for loc in error.get("loc", []))
        message = error.get("msg", "Validation error")
        error_messages.append(f"{field}: {message}")
    
    detail_message = "; ".join(error_messages)
    
    response = BaseResponse.failure(
        error_code=ErrorCode.INVALID_PARAMETER,
        detail_message=detail_message
    )
    
    return JSONResponse(
        status_code=ErrorCode.INVALID_PARAMETER.http_status.value,
        content=response.model_dump(by_alias=True)
    )


async def http_exception_handler(
    request: Request,
    exc: HTTPException
) -> JSONResponse:
    """HTTPException 핸들러 (FastAPI)"""
    # HTTPException의 status_code에 맞는 ErrorCode 찾기
    error_code = ErrorCode.INTERNAL_SERVER_ERROR
    
    if exc.status_code == status.HTTP_404_NOT_FOUND:
        error_code = ErrorCode.RESOURCE_NOT_FOUND
    elif exc.status_code == status.HTTP_401_UNAUTHORIZED:
        error_code = ErrorCode.UNAUTHORIZED
    elif exc.status_code == status.HTTP_403_FORBIDDEN:
        error_code = ErrorCode.FORBIDDEN
    elif exc.status_code == status.HTTP_405_METHOD_NOT_ALLOWED:
        error_code = ErrorCode.METHOD_NOT_ALLOWED
    
    response = BaseResponse.failure(
        error_code=error_code,
        detail_message=exc.detail
    )
    
    return JSONResponse(
        status_code=exc.status_code,
        content=response.model_dump(by_alias=True)
    )


async def starlette_http_exception_handler(
    request: Request,
    exc: StarletteHTTPException
) -> JSONResponse:
    """StarletteHTTPException 핸들러"""
    error_code = ErrorCode.INTERNAL_SERVER_ERROR
    
    if exc.status_code == 404:
        error_code = ErrorCode.RESOURCE_NOT_FOUND
    elif exc.status_code == 401:
        error_code = ErrorCode.UNAUTHORIZED
    elif exc.status_code == 403:
        error_code = ErrorCode.FORBIDDEN
    elif exc.status_code == 405:
        error_code = ErrorCode.METHOD_NOT_ALLOWED
    
    response = BaseResponse.failure(
        error_code=error_code,
        detail_message=exc.detail
    )
    
    return JSONResponse(
        status_code=exc.status_code,
        content=response.model_dump(by_alias=True)
    )


async def general_exception_handler(
    request: Request,
    exc: Exception
) -> JSONResponse:
    """일반 Exception 핸들러 (예상치 못한 오류)"""
    import traceback
    
    # 디버그 모드에서는 상세한 에러 정보 포함
    detail_message = str(exc)
    if hasattr(request.app, "debug") and request.app.debug:
        detail_message = f"{str(exc)}\n{traceback.format_exc()}"
    
    response = BaseResponse.failure(
        error_code=ErrorCode.INTERNAL_SERVER_ERROR,
        detail_message=detail_message
    )
    
    return JSONResponse(
        status_code=ErrorCode.INTERNAL_SERVER_ERROR.http_status.value,
        content=response.model_dump(by_alias=True)
    )

