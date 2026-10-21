"""외부 MCP·LLM만 대체한다. 네 SG와 상위 그래프·체크포인터는 실제로 실행한다."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from app.approval_registration import tools as approval_tools
from app.quote_drafting import tools as quote_tools
from app.rfq_review import mcp_tools, nodes
from app.rfq_review.state import CustomerQuestions, ParsedRFQ
from app.supply_analysis import deps
from app.supervisor.graph import build_graph
from app.supervisor.service import WorkflowService
from tests.quote_drafting.test_graph import FakeMcp
from tests.supply_analysis.conftest import FakeGateway


class ReviewModel:
    def __init__(self, environment, schema=None):
        self.environment = environment
        self.schema = schema

    def with_structured_output(self, schema, **kwargs):
        return ReviewModel(self.environment, schema)

    async def ainvoke(self, messages):
        if self.schema is ParsedRFQ:
            return ParsedRFQ.model_validate(self.environment.parsed)
        if self.schema is CustomerQuestions:
            return CustomerQuestions(questions=["요구 사항을 확인해 주세요."])
        return AIMessage("모델과 옵션이 요구 사양을 충족합니다.")


class QuoteModel:
    def __init__(self):
        self.calls = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        task = json.loads(messages[1].content)
        conditions = []
        for item in task["conditions"]:
            facts = item["facts"]
            if item["type"] == "SUBSTITUTE_PART":
                text = f"대체 부품 {facts['substitute_part_id']} {facts['qty']}개 적용, 추가 비용 {facts['extra_cost_krw']:,}원 별도."
            elif item["type"] == "ASSUMPTION":
                text = f"확인값 {facts['assumed_value']} 적용."
            else:
                text = "계약 조건 협의가 필요합니다."
            conditions.append({"index": item["index"], "text": text})
        return AIMessage("", tool_calls=[{"name": "submit_conditions", "args": {"conditions": conditions}, "id": "conditions"}])


class Environment:
    def __init__(self):
        fixture = Path(__file__).parents[1] / "quote_drafting/fixtures/1_parsed_rfq.json"
        self.parsed = json.loads(fixture.read_text())
        self.parsed["spec"].update(flow={"value": 22, "unit": "L/min"}, connector="표준 커넥터", coolant="혼합액")
        self.parsed["volume"]["lifetime_volume"] = 720000
        self.parsed["schedule"]["ppap_date"] = "2027-08-01"
        self.parsed["contract"].update(warranty_years=3, warranty_km=100000, penalty_cap_pct=10)
        self.gateway = FakeGateway()
        self.quote_mcp = FakeMcp()
        self.writer = QuoteModel()
        self.calls = []
        self.no_models = False
        self.fail_save = False
        self.save_status = "DRAFT"
        self.register_status = "REGISTERED"
        self.supply_transform = lambda supply: supply

    async def sg1_call(self, server, name, arguments):
        self.calls.append(("SG1", name, deepcopy(arguments)))
        if name == "get_rfq_content":
            return {"rfq_number": arguments["rfq_number"], "content": "RFQ 원문"}
        if name == "get_models":
            return [] if self.no_models else [{
                "model_id": "EWP-12-B", "flow_lpm": 25, "head_kpa": 80,
                "voltage_min": 9, "voltage_max": 16, "temp_min": -40, "temp_max": 100,
                "ip_rating": "IP67", "noise_db": 40, "comm": "LIN",
                "size_l_mm": 110, "size_w_mm": 90, "size_h_mm": 85, "weight_kg": 0.7,
                "base_price_krw": 34000,
            }]
        if name == "get_options":
            return {"model_id": "EWP-12-B", "options": [
                {"option_code": "OPT-HT", "changes_field": "temp_max", "new_value": "125", "add_price_krw": 2400},
                {"option_code": "OPT-IP9K", "changes_field": "ip_rating", "new_value": "IP6K9K", "add_price_krw": 900},
            ]}
        if name == "rag_search_docs":
            return []
        if name == "save_review":
            return {"review_id": 1, "thread_id": arguments["thread_id"], "status": "WAITING"}
        raise AssertionError(name)

    async def sg3_call(self, server, name, arguments):
        self.calls.append(("SG3", name, deepcopy(arguments)))
        if name == "save_quote":
            if self.fail_save:
                raise ConnectionError("저장 응답 유실")
            return {"quote_id": f"Q-{arguments['revision']}", "status": self.save_status}
        return await self.quote_mcp(server, name, arguments)

    async def sg4_call(self, server, name, arguments):
        self.calls.append(("SG4", name, deepcopy(arguments)))
        if name == "rag_search_docs":
            return []
        if name == "register_quote":
            return {"quote_id": arguments["quote_id"], "status": self.register_status, "outcome": "CREATED"}
        if name == "check_parts_supply":
            inventory = {p["part_id"]: p["available"] for p in self.gateway.supply["parts"]}
            inventory["P-MCU-G1-ALT"] = self.gateway.substitute["available"]
            parts = []
            for part in arguments["parts"]:
                available = inventory[part["part_id"]]
                short = max(0, part["required_qty"] - available)
                parts.append({**part, "available": available, "short_qty": short, "status": "SHORT" if short else "OK"})
            return self.supply_transform({"parts": list(reversed(parts))})
        raise AssertionError(name)

    def names(self, stage):
        return [name for s, name, _ in self.calls if s == stage]


@pytest.fixture
def workflow(monkeypatch):
    environment = Environment()
    monkeypatch.setattr(mcp_tools, "_call", environment.sg1_call)
    monkeypatch.setattr(nodes, "_llm", lambda: ReviewModel(environment))
    monkeypatch.setattr(deps, "gateway", lambda: environment.gateway)
    monkeypatch.setattr(quote_tools, "_call_once", environment.sg3_call)
    monkeypatch.setattr(approval_tools, "_call_tool", environment.sg4_call)
    graph = build_graph(quote_llm=environment.writer, quote_fallback_llm=environment.writer)
    return WorkflowService(graph), environment
