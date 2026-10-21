"""Business·RAG MCP 연결에 사용하는 공통 인증 헤더."""

from app.core.config import settings


def get_mcp_headers() -> dict[str, str]:
    return {
        "X-API-Key": settings.MCP_API_KEY or settings.INTERNAL_API_KEY,
        "X-Internal-Api-Key": settings.INTERNAL_API_KEY,
    }
