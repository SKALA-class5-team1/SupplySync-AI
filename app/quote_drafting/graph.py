"""SG3 견적 작성 서브그래프

START → prepare_quote ─┬→ agent ⇄ tools ─→ request_save_approval(멈춤) ─→ save_quote → END
                       └──────(조건 문구 없음)──↗          │ 수정 요청 → agent
어느 단계든 한도 초과·실패면 sg3_outcome=FAILED로 END.

호출하는 쪽(상위 그래프)이 checkpointer를 붙여 compile 한다. interrupt는 그 checkpointer로 멈추고,
같은 thread_id로 Command(resume=...)를 보내면 이어진다. 호출 config는 harness.run_config(thread_id).
"""
import logging
from typing import Optional

from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph

from app.core.rules import load_rules
from app.quote_drafting.harness import setup_tracing
from app.quote_drafting.nodes import QuoteDraftingNodes
from app.quote_drafting.schemas import SG3State

logger = logging.getLogger(__name__)


def build_graph(
    llm: Optional[Runnable] = None, fallback_llm: Optional[Runnable] = None, rules: Optional[dict] = None
) -> StateGraph:
    """SG3 그래프를 만든다 (compile 하지 않음). llm·fallback_llm을 안 주면 OpenAI 모델을 처음 쓸 때 만든다."""
    if setup_tracing():
        logger.info("SG3 LangSmith 추적 켜짐")
    nodes = QuoteDraftingNodes(rules or load_rules(), llm, fallback_llm)

    graph = StateGraph(SG3State)
    graph.add_node("prepare_quote", nodes.prepare_quote)
    graph.add_node("agent", nodes.agent)
    graph.add_node("tools", nodes.run_tools)
    graph.add_node("request_save_approval", nodes.request_save_approval)
    graph.add_node("save_quote", nodes.save_quote)

    graph.add_edge(START, "prepare_quote")
    graph.add_conditional_edges(
        "prepare_quote", nodes.after_prepare, {"agent": "agent", "request_save_approval": "request_save_approval", "end": END}
    )
    graph.add_conditional_edges("agent", nodes.after_agent, {"tools": "tools", "agent": "agent", "end": END})
    graph.add_conditional_edges(
        "tools", nodes.after_tools, {"request_save_approval": "request_save_approval", "agent": "agent", "end": END}
    )
    graph.add_conditional_edges(
        "request_save_approval", nodes.after_approval, {"save_quote": "save_quote", "agent": "agent", "end": END}
    )
    graph.add_edge("save_quote", END)
    return graph
