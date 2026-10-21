"""SG3 노드: draft_quote → validate_quote → save_quote"""
import logging
from datetime import date

from app.core.mcp_client import McpToolError
from app.quote_drafting.calc import (
    calc_margin_pct,
    calc_price_breakdown,
    calc_price_by_year,
    calc_unit_price,
    calc_valid_until,
)
from app.quote_drafting.checks import POLICY_RULES, run_checks
from app.quote_drafting.conditions import build_conditions
from app.quote_drafting.data_source import QuoteDataSource
from app.quote_drafting.state import QuoteDraftingState
from app.quote_drafting.writer import ConditionWriter

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class QuoteDraftingNodes:
    def __init__(self, source: QuoteDataSource, writer: ConditionWriter, rules: dict):
        self.source = source
        self.writer = writer
        self.rules = rules

    async def draft_quote(self, state: QuoteDraftingState) -> dict:
        rfq = state["parsed_rfq"]
        review = state["rfq_review"]
        model_id = review["selected_model"]
        options = review["options"]

        # 숫자: MCP 조회 + calc.py
        option_info = await self.source.get_options(model_id)
        option_prices = {o["option_code"]: o["add_price_krw"] for o in option_info["options"]}
        price_breakdown = calc_price_breakdown(model_id, option_info["base_price_krw"], option_prices, options)
        unit_price = calc_unit_price(price_breakdown)

        parts = await self.source.get_parts_cost(model_id, options)
        parts_cost = parts["parts_cost_krw"]
        years = rfq.get("volume", {}).get("supply_years") or self.rules["price_by_year"]["years"]
        price_by_year = calc_price_by_year(unit_price, parts_cost, rfq["commercial"]["annual_cr_pct"], years)

        issue_date = date.fromisoformat(state.get("issue_date") or date.today().isoformat())
        valid_until = calc_valid_until(issue_date, self.rules["quote_format"]["valid_days"])

        # 문구: 근거 검색 → writer
        drafts = build_conditions(rfq, review, state["supply_analysis"], self.rules)
        for d in drafts:
            if d.query:
                d.evidence = [
                    {"chunk_id": c["chunk_id"], "section": c["section"], "content": c["content"]}
                    for c in await self.source.search_docs(d.query, doc_type="policy", k=2)
                ]
        feedback = list(state.get("feedback") or [])
        if state.get("reject_reason"):
            feedback.append(f"승인 반려 사유: {state['reject_reason']}")
        texts = await self.writer.write(drafts, feedback)
        conditions = [{**d.condition, "text": text} for d, text in zip(drafts, texts)]

        demand = review["demand"]
        delivery = rfq["delivery"]
        quote = {
            "quote_id": None,
            "rfq_number": review["rfq_id"],
            # SG1 결과에 발행사(부서)가 있으면 쓰고, 없으면 고객사명
            "recipient": rfq.get("issuer") or rfq["customer"],
            "item": rfq["item"],
            "model_id": model_id,
            "options": options,
            "unit_price_krw": unit_price,
            "price_breakdown": price_breakdown,
            "parts_cost_krw": parts_cost,
            "parts_cost_breakdown": [
                {"part_id": p["part_id"], "cost_krw": p["cost_krw"]} for p in parts["parts_cost_breakdown"]
            ],
            "margin_pct": calc_margin_pct(unit_price, parts_cost),
            "price_by_year": price_by_year,
            "eau": demand["eau"],
            "sample_qty": demand["sample_qty"],
            "sample_due_date": demand["sample_due_date"],
            "sop_date": demand["sop_date"],
            "incoterms": f"{delivery['incoterms']} {delivery['plant']}",
            "issue_date": issue_date.isoformat(),
            "valid_until": valid_until.isoformat(),
            "conditions": conditions,
            "checks": [],
            "status": "DRAFT",
        }
        return {
            "quote_draft": quote,
            "condition_facts": [{**d.facts, "evidence": d.evidence} for d in drafts],
            "attempts": state.get("attempts", 0) + 1,
        }

    async def validate_quote(self, state: QuoteDraftingState) -> dict:
        checks = run_checks(
            state["quote_draft"], state["condition_facts"], state["parsed_rfq"], state["rfq_review"], self.rules
        )
        failed = [c for c in checks if c["result"] == "FAIL"]
        for c in failed:
            logger.info("SG3 검증 실패 %s: %s", c["rule"], c["value"])
        return {
            "quote_draft": {**state["quote_draft"], "checks": checks},
            "feedback": [f"{c['label']}({c['rule']}) 실패: {c['value']}" for c in failed],
        }

    async def save_quote(self, state: QuoteDraftingState) -> dict:
        quote = state["quote_draft"]
        payload = {
            "thread_id": state["thread_id"],
            "review_id": state["review_id"],
            **{k: v for k, v in quote.items() if k not in ("quote_id", "status")},
        }
        try:
            result = await self.source.save_quote(payload)
        except McpToolError as e:
            # 이미 APPROVED·REGISTERED인 견적(409) 등 → 그래프 FAILED
            return {"status": "FAILED", "error": str(e)}
        return {
            "quote_draft": {**quote, "quote_id": result["quote_id"], "status": result["status"]},
            "quote_id": result["quote_id"],
            "status": result["status"],
            "error": None,
        }

    async def fail_quote(self, state: QuoteDraftingState) -> dict:
        return {
            "status": "FAILED",
            "error": f"검증 실패가 {state['attempts']}회 재작성 후에도 남음: {state['feedback']}",
        }

    def route_after_validate(self, state: QuoteDraftingState) -> str:
        failed = {c["rule"] for c in state["quote_draft"]["checks"] if c["result"] == "FAIL"}
        if not failed - POLICY_RULES:
            # 모두 통과, 또는 문구로 고칠 수 없는 숫자 판정만 실패 → 저장하고 SG4 승인에서 판단
            return "save_quote"
        if state["attempts"] < MAX_ATTEMPTS:
            return "draft_quote"
        return "fail_quote"
