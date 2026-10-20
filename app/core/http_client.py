from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

from app.core.config import settings

# 비동기 클라이언트
async_client = httpx.AsyncClient(
    timeout=10.0,
    follow_redirects=True
)

# 동기 클라이언트
sync_client = httpx.Client(
    timeout=10.0,
    follow_redirects=True
)


@asynccontextmanager
async def get_internal_api_client() -> AsyncIterator[httpx.AsyncClient]:
    """Spring Boot의 /internal/** 호출 전용 클라이언트. 사용 후 자동으로 닫습니다."""
    backend_url = httpx.URL(settings.BACKEND_BASE_URL)

    async def add_internal_api_key(request: httpx.Request) -> None:
        # 내부 호출 키가 다른 서버나 공개 API로 전달되지 않도록 범위를 제한합니다.
        same_origin = (
            request.url.scheme == backend_url.scheme
            and request.url.host == backend_url.host
            and request.url.port == backend_url.port
        )
        internal_path = request.url.path == "/internal" or request.url.path.startswith("/internal/")
        if not same_origin or not internal_path:
            raise ValueError("내부 API 클라이언트는 설정된 백엔드의 /internal/** 경로만 호출할 수 있습니다.")
        request.headers["X-Internal-Api-Key"] = settings.INTERNAL_API_KEY

    async with httpx.AsyncClient(
        base_url=backend_url,
        timeout=10.0,
        follow_redirects=False,
        event_hooks={"request": [add_internal_api_key]},
    ) as client:
        yield client

