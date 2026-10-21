"""SG3 노드

prepare_quote(숫자: MCP 조회 + calc) → agent ⇄ tools(문구: 규정 검색·제출·검증)
→ request_save_approval(멈춤) → save_quote

숫자는 코드가 계산하고, LLM은 조건부 사항 문구만 쓴다. 한도는 harness.py 상수로 강제한다.
"""
import asyncio
import json
import logging
from datetime import date
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import Runnable
from langgraph.types import interrupt
from pydantic import ValidationError

from app.core.guardrails import mask_any, mask_pii
from app.quote_drafting import tools
from app.quote_drafting.calc import (
    calc_margin_pct,
    calc_price_breakdown,
    calc_price_by_year,
    calc_unit_price,
    calc_valid_until,
)
from app.quote_drafting.checks import POLICY_RULES, annual_cr_pct, needs_rewrite, run_checks
from app.quote_drafting.conditions import ConditionDraft, build_conditions
from app.quote_drafting.context import compact_messages
from app.quote_drafting.harness import (
    FALLBACK_MODEL,
    MAX_APPROVAL_INPUTS,
    MAX_SAME_TOOL_CALLS,
    MAX_TURNS,
    PRIMARY_MODEL,
    TASK_TIMEOUT_SEC,
    describe_error,
    load_prompt,
)
from app.quote_drafting.model import bind_llm_tools, create_chat_model, invoke_with_recovery
from app.quote_drafting.schemas import (
    DocHit,
    OptionsResult,
    PartsCostResult,
    SaveApprovalCard,
    SG3State,
)
from app.quote_drafting.tools import ConditionText

logger = logging.getLogger(__name__)

SAVE_APPROVAL_EVENT = "SAVE_APPROVAL_REQUIRED"
SAVE_APPROVAL_STEP = "SG3.request_save_approval"  # resume 요청의 step 값 (SG4는 "SG4.request_approval")
# 최종 반려(REJECTED)는 견적이 저장된 뒤 SG4에서만 한다. SG3 단계의 거부는 수정 요청(REVISE)으로 재작성한다.
_DECISION_RESULTS = {"APPROVED", "REVISE"}
_SUBMIT_REMINDER = "작업은 submit_conditions 도구 호출로 끝내야 한다. 모든 index의 문구를 submit_conditions로 제출하라."


def _now() -> float:
    return asyncio.get_running_loop().time()


def _failed(state: SG3State, what: str, exc: Optional[BaseException] = None, **extra) -> dict[str, Any]:
    """FAILED로 끝낸다. 오류 내용은 개인정보를 가려서 남긴다."""
    error = f"{what}: {describe_error(exc)}" if exc else what
    logger.error("SG3 FAILED trace_id=%s %s", state.thread_id, error)
    return {**extra, "sg3_outcome": "FAILED", "error": error, "ready_to_save": False}


def _target_price(state: SG3State) -> Optional[int]:
    """목표가. SG1 demand에 있고, 없으면 RFQ 파싱 결과에서 읽는다. 둘 다 없으면 None."""
    commercial = state.parsed_rfq.get("commercial") or {}
    return state.demand.get("target_price_krw") or commercial.get("target_price_krw")


def _assemble_quote(state: SG3State, option_info: OptionsResult, parts: PartsCostResult, rules: dict) -> dict:
    # SG1 모델(ParsedRFQ, demand)은 값이 모두 Optional이라 비어 있을 수 있다. 빈 필수 항목은 FORMAT 검증이 잡는다.
    rfq = state.parsed_rfq
    model_id = state.selected_model
    options = state.options

    option_prices = {o.option_code: o.add_price_krw for o in option_info.options}
    price_breakdown = calc_price_breakdown(model_id, option_info.base_price_krw, option_prices, options)
    unit_price = calc_unit_price(price_breakdown)
    parts_cost = parts.parts_cost_krw
    years = (rfq.get("volume") or {}).get("supply_years") or rules["price_by_year"]["years"]
    issue_date = date.fromisoformat(state.issue_date or date.today().isoformat())

    demand = state.demand
    delivery = rfq.get("delivery") or {}
    incoterms = " ".join(v for v in (delivery.get("incoterms"), delivery.get("plant")) if v)
    return {
        "quote_id": None,
        "rfq_number": state.rfq_number,
        "recipient": rfq.get("customer"),  # SG1 parse_rfq의 고객사
        "item": rfq.get("item"),
        "model_id": model_id,
        "options": options,
        "unit_price_krw": unit_price,
        "price_breakdown": price_breakdown,
        "parts_cost_krw": parts_cost,
        "parts_cost_breakdown": [{"part_id": p.part_id, "cost_krw": p.cost_krw} for p in parts.parts_cost_breakdown],
        "margin_pct": calc_margin_pct(unit_price, parts_cost),
        "price_by_year": calc_price_by_year(unit_price, parts_cost, annual_cr_pct(rfq), years),
        "eau": demand.get("eau"),
        "sample_qty": demand.get("sample_qty"),
        "sample_due_date": demand.get("sample_due_date"),
        "sop_date": demand.get("sop_date"),
        "incoterms": incoterms or None,
        "issue_date": issue_date.isoformat(),
        "valid_until": calc_valid_until(issue_date, rules["quote_format"]["valid_days"]).isoformat(),
        "conditions": [],
        "checks": [],
        "status": "DRAFT",
    }


def _approval_card(quote: dict) -> SaveApprovalCard:
    """사람이 확인할 내용: 검증 실패(숫자 판정 미달·필수 항목 누락)와 대체 부품·계약 수정 제안."""
    items = [
        f"{c['label']} 기준 미달 ({c['value']}){' ' + c['ref'] if c.get('ref') else ''} - 승인 필요"
        if c["rule"] in POLICY_RULES
        else f"{c['label']} 확인 필요 ({c['value']})"
        for c in quote["checks"]
        if c["result"] == "FAIL"
    ]
    items += [c["text"] for c in quote["conditions"] if c["type"] in ("SUBSTITUTE_PART", "CONTRACT_PROPOSAL")]
    summary = f"{' + '.join([quote['model_id'], *quote['options']])}, 단가 {quote['unit_price_krw']:,}원"
    return SaveApprovalCard(quote_summary=mask_pii(summary), approval_items=mask_any(items), checks=quote["checks"])


def _parse_save_decision(value: Any) -> dict[str, Any]:
    """저장 승인 resume 본문을 읽는다. 잘못된 입력은 ValueError. 승인으로 짐작하지 않는다.

    SG4 resume과 같은 형식이고 step만 다르다 (실행 서비스가 형식 하나로 보내도록):
      {"step": "SG3.request_save_approval", "userId": 2,
       "decision": {"result": "APPROVED" | "REVISE", "reason": "...", "changes": {}}}
    - step 이 지금 대기 중인 단계와 다르면 받지 않는다 (SG4 단계의 응답이 잘못 오는 일 방지).
    - REVISE(문구 수정 요청)에는 reason 이 있어야 한다.
    - REJECTED는 받지 않는다. 최종 반려는 SG4 단계에서만 한다 (견적이 없으면 BE가 RUN_END(REJECTED)를 막음).
    - 승인자 권한 검사와 기록은 백엔드가 한다. 여기서는 userId 를 남기기만 한다.
    """
    if not isinstance(value, dict):
        raise ValueError("승인 결과는 객체여야 합니다")
    if value.get("step") != SAVE_APPROVAL_STEP:
        raise ValueError(f"step 은 {SAVE_APPROVAL_STEP} 여야 합니다")
    user_id = value.get("userId")
    if isinstance(user_id, bool) or not isinstance(user_id, (int, str)) or user_id == "":
        raise ValueError("userId 가 필요합니다")
    decision = value.get("decision")
    if not isinstance(decision, dict):
        raise ValueError("decision 은 객체여야 합니다")
    result = decision.get("result")
    if result not in _DECISION_RESULTS:
        raise ValueError("decision.result 는 APPROVED, REVISE 중 하나여야 합니다 (최종 반려는 SG4 승인 단계에서)")
    reason = decision.get("reason")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("decision.reason 은 문자열이어야 합니다")
    reason = reason.strip() if reason else None
    if result != "APPROVED" and not reason:
        raise ValueError("수정 요청에는 decision.reason(사유)이 필요합니다")
    changes = decision.get("changes") or {}
    if not isinstance(changes, dict):
        raise ValueError("decision.changes 는 객체여야 합니다")
    return {"result": result, "reason": reason, "changes": changes, "user_id": user_id}


class QuoteDraftingNodes:
    def __init__(self, rules: dict, llm: Optional[Runnable] = None, fallback_llm: Optional[Runnable] = None):
        self.rules = rules
        self._llm = llm
        self._fallback_llm = fallback_llm
        self.prompt_version, self.prompt = load_prompt()

    def _models(self) -> tuple[Runnable, Runnable]:
        # 실제 모델은 처음 쓸 때 만든다 (OPENAI_API_KEY 없이도 그래프 생성·테스트가 되도록)
        if self._llm is None:
            self._llm = create_chat_model(PRIMARY_MODEL)
        if self._fallback_llm is None:
            self._fallback_llm = create_chat_model(FALLBACK_MODEL)
        return bind_llm_tools(self._llm), bind_llm_tools(self._fallback_llm)

    # --- 숫자 ---

    async def prepare_quote(self, state: SG3State) -> dict[str, Any]:
        started = _now()
        deadline = started + TASK_TIMEOUT_SEC
        logger.info(
            "SG3 시작 trace_id=%s prompt=%s revision=%d 직전 quote_id=%s",
            state.thread_id, self.prompt_version, state.draft_revision, state.quote_id,
        )
        if not state.selected_model:
            # SG1이 맞는 모델을 고르지 못함 (fit_type 신규 개발 등). 단가를 낼 모델이 없다.
            return _failed(state, "SG1 선정 모델(selected_model)이 없어 견적을 작성할 수 없습니다")
        try:
            option_info = await tools.business_get_options(state.selected_model, deadline=deadline)
            parts = await tools.business_get_parts_cost(state.selected_model, state.options, deadline=deadline)
            quote = _assemble_quote(state, option_info, parts, self.rules)
            drafts = build_conditions(
                state.parsed_rfq, state.assumptions, state.contract_risks, state.supply_analysis, self.rules
            )
        except Exception as exc:  # noqa: BLE001 - 재시도 후에도 실패하면 FAILED
            return _failed(state, "단가·원가 조회 실패", exc, elapsed_sec=_now() - started)

        quote["conditions"] = [d.condition for d in drafts]
        # 반려 후 다시 들어오는 경우를 위해 이전 실행의 작업 값과 결과를 비운다
        update: dict[str, Any] = {
            "quote_draft": quote,
            "condition_facts": [d.facts for d in drafts],
            "turns": 0,
            "tool_calls": {},
            "evidence": [],
            "save_approval_card": None,
            "sg3_outcome": None,
            "error": None,
        }
        if not drafts:
            # 쓸 문구가 없으면 모델을 부르지 않고 바로 검증 후 저장 승인으로 간다
            quote["checks"] = run_checks(quote, [], state.parsed_rfq, _target_price(state), self.rules)
            update.update(messages=[], ready_to_save=True, elapsed_sec=_now() - started)
            return update

        update.update(
            messages=[SystemMessage(self.prompt), self._task_message(drafts, state.reject_reason, state.changes)],
            ready_to_save=False,
            elapsed_sec=_now() - started,
        )
        return update

    @staticmethod
    def _task_message(drafts: list[ConditionDraft], reject_reason: Optional[str], changes: dict) -> HumanMessage:
        # SG4 수정 요청(REVISE)으로 다시 들어오면 사유와 요청한 수정 내용을 feedback으로 넘긴다
        feedback = []
        if reject_reason:
            feedback.append(f"담당자 수정 요청: {reject_reason}")
        if changes:
            feedback.append(f"담당자가 요청한 수정 내용: {json.dumps(changes, ensure_ascii=False)}")
        task = {
            "conditions": [
                {"index": i, "type": d.condition["type"], "facts": d.facts} for i, d in enumerate(drafts)
            ],
            "feedback": feedback,
        }
        return HumanMessage(json.dumps(mask_any(task), ensure_ascii=False))

    # --- 문구 Agent Loop ---

    async def agent(self, state: SG3State) -> dict[str, Any]:
        started = _now()
        if state.turns >= MAX_TURNS:
            return _failed(state, f"모델 호출 {MAX_TURNS}턴 초과")
        if state.elapsed_sec >= TASK_TIMEOUT_SEC:
            return _failed(state, f"작업 시간 {TASK_TIMEOUT_SEC:.0f}초 초과")

        messages = compact_messages(state.messages)
        try:
            primary, fallback = self._models()
            reply = await invoke_with_recovery(
                primary, fallback, messages, started + TASK_TIMEOUT_SEC - state.elapsed_sec, state.thread_id
            )
        except Exception as exc:  # noqa: BLE001 - 재시도·대체 모델 후에도 실패하면 FAILED
            return _failed(state, "모델 호출 실패", exc, elapsed_sec=state.elapsed_sec + _now() - started)

        messages = [*messages, reply]
        if not reply.tool_calls:
            messages.append(HumanMessage(_SUBMIT_REMINDER))
        logger.info(
            "SG3 turn %d trace_id=%s tools=%s", state.turns + 1, state.thread_id, [c["name"] for c in reply.tool_calls]
        )
        return {"messages": messages, "turns": state.turns + 1, "elapsed_sec": state.elapsed_sec + _now() - started}

    async def run_tools(self, state: SG3State) -> dict[str, Any]:
        """모델이 고른 도구를 실행한다. 도구 오류는 예외로 끝내지 않고 ToolMessage로 모델에 돌려준다."""
        started = _now()
        deadline = started + TASK_TIMEOUT_SEC - state.elapsed_sec
        last: AIMessage = state.messages[-1]
        counts = dict(state.tool_calls)
        evidence = list(state.evidence)
        quote = state.quote_draft
        ready = False
        replies = []

        for call in last.tool_calls:
            name = call["name"]
            counts[name] = counts.get(name, 0) + 1
            if counts[name] > MAX_SAME_TOOL_CALLS:
                return _failed(
                    state, f"같은 도구({name}) {MAX_SAME_TOOL_CALLS}회 초과",
                    tool_calls=counts, elapsed_sec=state.elapsed_sec + _now() - started,
                )
            status = "success"
            try:
                if name == "search_policy_docs":
                    hits = await tools.rag_search_docs(call["args"]["query"], deadline=deadline)
                    known = {e.chunk_id for e in evidence}
                    evidence += [h for h in hits if h.chunk_id not in known]
                    content = json.dumps([h.model_dump() for h in hits], ensure_ascii=False) if hits else "검색 결과 없음"
                elif name == "submit_conditions":
                    content, quote, ready = self._review_submission(state, call["args"], evidence)
                else:
                    raise ValueError(f"알 수 없는 도구: {name}")
            except Exception as exc:  # noqa: BLE001 - 도구 오류는 모델이 다시 판단하도록 돌려준다
                status = "error"
                content = f"도구 오류 - {describe_error(exc)}. 이 내용을 보고 다시 판단하라."
                logger.warning("SG3 도구 오류 trace_id=%s %s %s", state.thread_id, name, content)
            replies.append(ToolMessage(mask_pii(content), tool_call_id=call["id"], name=name, status=status))

        return {
            "messages": [*state.messages, *replies],
            "tool_calls": counts,
            "evidence": evidence,
            "quote_draft": quote,
            "ready_to_save": ready,
            "elapsed_sec": state.elapsed_sec + _now() - started,
        }

    def _review_submission(self, state: SG3State, args: dict, evidence: list[DocHit]) -> tuple[str, dict, bool]:
        """제출된 문구를 넣고 검증 5항목을 돌린다. (도구 응답, 갱신된 견적, 저장 단계로 갈 수 있는지)"""
        quote = state.quote_draft
        try:
            texts = [ConditionText.model_validate(c) for c in args.get("conditions", [])]
        except ValidationError as exc:
            return f"형식 오류 - conditions는 [{{index, text}}] 목록이어야 한다: {exc.error_count()}건", quote, False
        expected = set(range(len(quote["conditions"])))
        by_index = {t.index: t.text for t in texts}
        if set(by_index) != expected:
            return f"형식 오류 - index {sorted(expected)}를 빠짐없이 한 번씩 제출하라 (받은 index: {sorted(by_index)})", quote, False

        conditions = [{**c, "text": mask_pii(by_index[i])} for i, c in enumerate(quote["conditions"])]
        draft = {**quote, "conditions": conditions}
        seen = [e.model_dump() for e in evidence]
        facts = [{**f, "evidence": seen} for f in state.condition_facts]
        draft["checks"] = run_checks(draft, facts, state.parsed_rfq, _target_price(state), self.rules)

        rewritable = needs_rewrite(draft["checks"], conditions)
        if rewritable:
            reasons = "; ".join(f"{c['label']}({c['rule']}): {c['value']}" for c in rewritable)
            return f"검증 실패 - 고쳐서 다시 제출하라: {reasons}", draft, False
        return "검증 통과. 담당자 저장 승인을 기다린다.", draft, True

    # --- 저장 (변경 도구: 사람 승인 후에만) ---

    async def request_save_approval(self, state: SG3State) -> dict[str, Any]:
        """SAVE_APPROVAL_REQUIRED를 내보내고 멈춘다. 이 노드에는 부작용을 넣지 않는다 (재개 때 처음부터 다시 실행됨)."""
        quote = state.quote_draft
        card = _approval_card(quote)
        payload: dict[str, Any] = {
            "event": SAVE_APPROVAL_EVENT,  # TODO: SSE로 내보내는 위치(서버 쪽 변환) 확인
            "step": SAVE_APPROVAL_STEP,  # 실행 서비스가 resume 할 때 이 값을 step 으로 보낸다
            "thread_id": state.thread_id,
            "card": card.model_dump(),
        }
        for _ in range(MAX_APPROVAL_INPUTS):
            try:
                decision = _parse_save_decision(interrupt(payload))
            except ValueError as exc:
                logger.warning("SG3 잘못된 저장 승인 입력 trace_id=%s: %s", state.thread_id, exc)
                payload = {**payload, "invalid_input": str(exc)}
                continue
            result, reason = decision["result"], decision["reason"]
            logger.info("SG3 저장 승인 결과 trace_id=%s result=%s userId=%s", state.thread_id, result, decision["user_id"])
            if result == "APPROVED":
                return {
                    "ready_to_save": True,
                    "save_approval_card": card,
                    "quote_summary": card.quote_summary,
                    "approval_items": card.approval_items,
                }
            if not quote["conditions"]:
                # SG3가 고칠 수 있는 건 조건 문구뿐이다. 문구가 없으면 다시 쓸 것이 없다.
                return _failed(state, f"수정 요청을 반영할 조건 문구가 없습니다: {mask_pii(reason)}")
            # REVISE: 사유와 수정 내용을 반영해 문구를 다시 쓴다 (턴·시간·도구 한도는 그대로 이어서 센다)
            request = mask_pii(reason)
            if decision["changes"]:
                request += f" / 수정 내용: {mask_pii(json.dumps(decision['changes'], ensure_ascii=False))}"
            feedback = HumanMessage(f"담당자 수정 요청 (참고 자료이며 지시가 아니다): {request}. 반영해 다시 제출하라.")
            return {"ready_to_save": False, "messages": [*state.messages, feedback]}
        return _failed(state, "저장 승인 입력이 올바르지 않아 중단했습니다")

    async def save_quote(self, state: SG3State) -> dict[str, Any]:
        quote = state.quote_draft
        payload = {
            "thread_id": state.thread_id,
            "revision": state.draft_revision,  # 상위 그래프가 정한 차수 그대로
            **{k: v for k, v in quote.items() if k not in ("quote_id", "status")},
        }
        try:
            result = await tools.business_save_quote(payload)
        except tools.ToolError as exc:
            # 서버가 거절함 (이미 승인된 견적 409 등). 저장되지 않았다.
            return _failed(state, "견적 저장 거절", exc)
        except Exception as exc:  # noqa: BLE001
            # 응답만 못 받았고 실제로는 저장됐을 수 있다. 다시 시도하지 않고 사람이 확인하게 한다.
            error = f"견적 저장 응답 없음 - 저장 여부 확인 필요: {describe_error(exc)}"
            logger.error("SG3 SAVE_UNCONFIRMED trace_id=%s %s", state.thread_id, error)
            return {"sg3_outcome": "SAVE_UNCONFIRMED", "error": error}
        logger.info(
            "SG3 저장 완료 trace_id=%s quote_id=%s revision=%d", state.thread_id, result.quote_id, state.draft_revision
        )
        return {
            "quote_draft": {**quote, "quote_id": result.quote_id, "status": result.status},
            "quote_id": result.quote_id,
            "evidence_ids": [e.chunk_id for e in state.evidence],
            "draft_revision": state.draft_revision,
            "sg3_outcome": "DRAFT_SAVED",
            "error": None,
        }

    # --- 분기 ---

    @staticmethod
    def after_prepare(state: SG3State) -> str:
        if state.sg3_outcome == "FAILED":
            return "end"
        return "request_save_approval" if state.ready_to_save else "agent"

    @staticmethod
    def after_agent(state: SG3State) -> str:
        if state.sg3_outcome == "FAILED":
            return "end"
        last = state.messages[-1]
        return "tools" if isinstance(last, AIMessage) and last.tool_calls else "agent"

    @staticmethod
    def after_tools(state: SG3State) -> str:
        if state.sg3_outcome == "FAILED":
            return "end"
        return "request_save_approval" if state.ready_to_save else "agent"

    @staticmethod
    def after_approval(state: SG3State) -> str:
        if state.sg3_outcome == "FAILED":
            return "end"
        return "save_quote" if state.ready_to_save else "agent"
