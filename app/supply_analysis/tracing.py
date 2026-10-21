"""공통 traced_node 도입 전 SG2 노드 실행 로그. 입력/키/문서 원문은 기록하지 않는다."""

import logging
from functools import wraps
from time import monotonic

logger = logging.getLogger(__name__)


def traced_node(graph: str, node: str):
    def decorate(function):
        @wraps(function)
        async def wrapper(state, *args, **kwargs):
            started = monotonic()
            logger.info("node_start graph=%s node=%s thread_id=%s", graph, node, state.get("thread_id"))
            try:
                result = await function(state, *args, **kwargs)
            except Exception:
                logger.error("node_error graph=%s node=%s elapsed_ms=%.1f", graph, node, (monotonic() - started) * 1000)
                raise
            logger.info("node_end graph=%s node=%s next_step=%s elapsed_ms=%.1f", graph, node, result.get("next_step"), (monotonic() - started) * 1000)
            return result

        return wrapper

    return decorate
