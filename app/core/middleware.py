"""
커스텀 미들웨어
"""
import time
import uuid
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp
import logging

logger = logging.getLogger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    요청별 고유 ID 생성 미들웨어
    
    각 요청에 고유한 ID를 부여하여 로그 추적을 용이하게 합니다.
    """
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 요청 ID 생성
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        
        # 응답 헤더에 요청 ID 추가
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        
        return response


class LoggingMiddleware(BaseHTTPMiddleware):
    """
    요청/응답 자동 로깅 미들웨어
    
    모든 요청과 응답을 자동으로 로깅합니다.
    """
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 요청 시작 시간
        start_time = time.time()
        
        # 요청 정보 로깅
        request_id = getattr(request.state, "request_id", "unknown")
        logger.info(
            f"[{request_id}] {request.method} {request.url.path} - "
            f"Client: {request.client.host if request.client else 'unknown'}"
        )
        
        # 요청 본문 로깅 (GET 요청이 아니고 작은 본문인 경우만)
        if request.method != "GET" and request.headers.get("content-length"):
            try:
                content_length = int(request.headers.get("content-length", 0))
                if content_length < 1000:  # 1KB 미만만 로깅
                    body = await request.body()
                    if body:
                        logger.debug(f"[{request_id}] Request body: {body.decode('utf-8')[:500]}")
            except Exception:
                pass
        
        # 응답 처리
        try:
            response = await call_next(request)
        except Exception as e:
            # 예외 발생 시 로깅
            process_time = time.time() - start_time
            logger.error(
                f"[{request_id}] {request.method} {request.url.path} - "
                f"Error: {str(e)} - Process time: {process_time:.3f}s"
            )
            raise
        
        # 응답 정보 로깅
        process_time = time.time() - start_time
        logger.info(
            f"[{request_id}] {request.method} {request.url.path} - "
            f"Status: {response.status_code} - Process time: {process_time:.3f}s"
        )
        
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    보안 헤더 추가 미들웨어
    
    응답에 보안 관련 HTTP 헤더를 추가합니다.
    """
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        
        # 보안 헤더 추가
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        
        return response

