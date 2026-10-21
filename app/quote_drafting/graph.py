"""SG3 견적 작성 서브그래프

START → draft_quote → validate_quote ─┬→ save_quote → END
                         ↑            ├→ draft_quote (문구·형식 실패, MAX_ATTEMPTS까지)
                         └────────────┘→ fail_quote → END
"""
from langgraph.graph import END, START, StateGraph

from app.core.rules import load_rules
from app.quote_drafting.data_source import QuoteDataSource
from app.quote_drafting.nodes import QuoteDraftingNodes
from app.quote_drafting.state import QuoteDraftingState
from app.quote_drafting.writer import ConditionWriter


def build_quote_drafting_graph(source: QuoteDataSource, writer: ConditionWriter, rules: dict | None = None):
    nodes = QuoteDraftingNodes(source, writer, rules or load_rules())

    graph = StateGraph(QuoteDraftingState)
    graph.add_node("draft_quote", nodes.draft_quote)
    graph.add_node("validate_quote", nodes.validate_quote)
    graph.add_node("save_quote", nodes.save_quote)
    graph.add_node("fail_quote", nodes.fail_quote)

    graph.add_edge(START, "draft_quote")
    graph.add_edge("draft_quote", "validate_quote")
    graph.add_conditional_edges(
        "validate_quote", nodes.route_after_validate, ["save_quote", "draft_quote", "fail_quote"]
    )
    graph.add_edge("save_quote", END)
    graph.add_edge("fail_quote", END)
    return graph.compile()
