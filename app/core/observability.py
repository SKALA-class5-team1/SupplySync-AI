"""LangSmith 추적 설정 (Harness: Observability).

- thread_id 하나를 trace_id 로 써서, 로그·SSE 이벤트·LangSmith 에서 같은 실행을 찾는다.
- LANGSMITH_TRACING=true 와 LANGSMITH_API_KEY 가 있을 때만 켜진다. 없으면 아무 일도 하지 않는다.
"""
import os
from typing import Any

from app.core.config import settings


def setup_tracing() -> bool:
    """설정값을 LangChain 이 읽는 환경변수로 옮긴다. 켜졌으면 True."""
    if not (settings.LANGSMITH_TRACING and settings.LANGSMITH_API_KEY):
        return False
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.LANGSMITH_API_KEY
    os.environ["LANGSMITH_PROJECT"] = settings.LANGSMITH_PROJECT
    return True


def run_config(thread_id: str, run_name: str = "supplysync") -> dict[str, Any]:
    """그래프 호출(ainvoke/astream)에 넘길 config. 같은 thread_id 로 재개해야 상태가 이어진다."""
    return {
        "configurable": {"thread_id": thread_id},
        "run_name": run_name,
        "tags": ["supplysync"],
        "metadata": {"trace_id": thread_id},
    }
