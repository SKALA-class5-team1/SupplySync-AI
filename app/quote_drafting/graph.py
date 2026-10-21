"""SG3 견적 작성 서브그래프

START → prepare_quote ─┬→ agent ⇄ tools ─→ save_quote → END
                       └──────(조건 문구 없음)──↗
어느 단계든 한도 초과·실패면 sg3_outcome=FAILED로 END.

검증이 끝나면 바로 DRAFT를 저장한다. 담당자 승인은 SG4에서만 받는다.
호출 config는 app.core.observability.run_config(thread_id, harness.RUN_NAME). LangSmith 설정은 main.py가 켠다.
"""
from typing import Optional

from langchain_core.runnables import Runnable
from langgraph.graph import END, START, StateGraph

from app.core.rules import load_rules
from app.quote_drafting.nodes import QuoteDraftingNodes
from app.quote_drafting.schemas import SG3State


def build_graph(
    llm: Optional[Runnable] = None, fallback_llm: Optional[Runnable] = None, rules: Optional[dict] = None
) -> StateGraph:
    """SG3 그래프를 만든다 (compile 하지 않음). llm·fallback_llm을 안 주면 OpenAI 모델을 처음 쓸 때 만든다."""
    nodes = QuoteDraftingNodes(rules or load_rules(), llm, fallback_llm)

    graph = StateGraph(SG3State)
    graph.add_node("prepare_quote", nodes.prepare_quote)
    graph.add_node("agent", nodes.agent)
    graph.add_node("tools", nodes.run_tools)
    graph.add_node("save_quote", nodes.save_quote)

    graph.add_edge(START, "prepare_quote")
    graph.add_conditional_edges(
        "prepare_quote", nodes.after_prepare, {"agent": "agent", "save_quote": "save_quote", "end": END}
    )
    graph.add_conditional_edges("agent", nodes.after_agent, {"tools": "tools", "agent": "agent", "end": END})
    graph.add_conditional_edges(
        "tools", nodes.after_tools, {"save_quote": "save_quote", "agent": "agent", "end": END}
    )
    graph.add_edge("save_quote", END)
    return graph
