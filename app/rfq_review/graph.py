"""
SG1 (RFQ·제품 검토) 서브그래프 조립.

parse_rfq → validate_rules
validate_rules → (미해결 HIGH) human_confirm → validate_rules
validate_rules → (HIGH 없음) match_spec
match_spec → (신규 개발) build_report → END
match_spec → (후보 있음) check_contract → build_report → END
"""
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.rfq_review import nodes
from app.rfq_review.state import SG1State

_DEFAULT = object()


def build_graph(checkpointer: Any = _DEFAULT):
    """
    SG1 그래프를 컴파일해 반환합니다.

    - checkpointer를 넘기지 않으면 MemorySaver를 씁니다 (서버 재시작 시 멈춘 상태가 사라짐).
    - 상위 그래프의 노드로 붙일 때 checkpointer=None을 넘기면 상위 그래프의 체크포인터를 이어받습니다.
    """
    graph = StateGraph(SG1State)
    graph.add_node("parse_rfq", nodes.parse_rfq)
    graph.add_node("validate_rules", nodes.validate_rules)
    graph.add_node("human_confirm", nodes.human_confirm)
    graph.add_node("match_spec", nodes.match_spec)
    graph.add_node("check_contract", nodes.check_contract)
    graph.add_node("build_report", nodes.build_report)

    graph.add_edge(START, "parse_rfq")
    graph.add_edge("parse_rfq", "validate_rules")
    graph.add_conditional_edges("validate_rules", nodes.route_after_validate, ["human_confirm", "match_spec"])
    graph.add_edge("human_confirm", "validate_rules")
    graph.add_conditional_edges("match_spec", nodes.route_after_match, ["check_contract", "build_report"])
    graph.add_edge("check_contract", "build_report")
    graph.add_edge("build_report", END)

    if checkpointer is _DEFAULT:
        checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)
