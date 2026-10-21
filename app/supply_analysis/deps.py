"""SG2 의존성 교체 지점. 공통 harness가 생기면 이 모듈의 구현을 교체한다."""

from functools import lru_cache
from typing import Any, Protocol


class Gateway(Protocol):
    async def call(self, name: str, **arguments: Any) -> Any: ...


@lru_cache(maxsize=1)
def gateway() -> Gateway:
    from app.supply_analysis.mcp_tools import McpGateway

    return McpGateway()
