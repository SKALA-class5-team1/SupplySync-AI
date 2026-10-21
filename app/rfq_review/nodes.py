"""
SG1 노드.

LLM(gpt-4o-mini, temperature 0)은 parse_rfq 추출, match_spec 설명 문장, build_report 질의서에만 씁니다.
판정과 숫자 계산은 rules.py에서 합니다. MCP 호출은 mcp_tools.py를 통해서만 합니다.
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.types import interrupt

from app.core.config import settings
from app.rfq_review import mcp_tools, rules
from app.rfq_review.state import (
    Assumption,
    ContractRisk,
    CustomerQuestions,
    Evidence,
    ParsedRFQ,
    SG1State,
)

logger = logging.getLogger(__name__)

LLM_MODEL = "gpt-4o-mini"
EXCERPT_CHARS = 120
EVIDENCE_K = 2


def _llm() -> ChatOpenAI:
    """노드 실행 시점에 생성합니다 (키가 없어도 서버·그래프 import는 가능)."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY가 .env에 설정되어 있지 않습니다.")
    return ChatOpenAI(model=LLM_MODEL, temperature=0, api_key=settings.OPENAI_API_KEY)


def _to_evidence(chunks: list[mcp_tools.DocChunk]) -> list[dict]:
    return [
        Evidence(chunk_id=c.chunk_id, section=c.section, excerpt=c.content[:EXCERPT_CHARS]).model_dump()
        for c in chunks
    ]


async def _evidence(query: str, doc_type: str, k: int = EVIDENCE_K) -> list[dict]:
    return _to_evidence(await mcp_tools.search_docs(query, doc_type, k))


def _demand(parsed: dict) -> dict:
    return {
        "eau": rules.get_path(parsed, "volume.eau"),
        "sample_qty": rules.get_path(parsed, "volume.sample_qty"),
        "sample_due_date": rules.get_path(parsed, "volume.sample_due_date"),
        "sop_date": rules.get_path(parsed, "schedule.sop_date"),
        "target_price_krw": rules.get_path(parsed, "commercial.target_price_krw"),
        "quote_due_date": parsed.get("quote_due_date"),
    }


def _review_payload(state: SG1State, status: str, **values) -> dict:
    """
    save_review 입력 (백엔드 합의 형식). 그 시점까지 나온 값만 넣습니다.
    값이 None인 필드는 보내지 않습니다 (빈 배열은 "결과 없음"이라 유지).
    """
    payload = {
        "thread_id": state["thread_id"],
        "rfq_number": state["rfq_number"],
        "stage": "SG1",
        "status": status,
        "issues": state.get("issues", []),
        "passed_checks": state.get("passed_checks", []),
        "assumptions": state.get("assumptions", []),
        "demand": _demand(state.get("parsed_rfq", {})),
    }
    payload.update(values)
    return {k: v for k, v in payload.items() if v is not None}


# ---------------------------------------------------------------------------
# 1. parse_rfq
# ---------------------------------------------------------------------------
PARSE_SYSTEM = """당신은 자동차 부품사 영업팀의 RFQ(견적요청서) 분석 담당입니다.
RFQ 원문에서 아래 JSON 양식의 값을 추출합니다.

규칙:
- 원문에 적힌 값만 추출합니다. 추측하거나 계산하지 않습니다.
- 단위는 원문 그대로 둡니다. 예: 유량이 '22 L/h'면 value=22, unit='L/h' (L/min으로 바꾸지 않음).
- 원문에 없거나 'TBD'면 null로 둡니다.
- 날짜는 YYYY-MM-DD, 숫자는 쉼표 없는 숫자로 씁니다.
- payment_days는 결제 기간 일수입니다 (예: '익월 60일' → 60).
- comm은 통신 방식 이름만 씁니다 (예: 'LIN 2.2' → 'LIN').
- 배상 상한이 '없음'이면 penalty_cap_pct는 null입니다.

추출 양식:
{schema}"""


async def parse_rfq(state: SG1State) -> dict:
    rfq = await mcp_tools.get_rfq_content(state["rfq_number"])
    extractor = _llm().with_structured_output(ParsedRFQ, method="function_calling")
    result: ParsedRFQ = await extractor.ainvoke([
        SystemMessage(PARSE_SYSTEM.format(schema=rules.load_rfq_schema_text())),
        HumanMessage(rfq.content),
    ])
    parsed = result.model_dump()
    if not parsed.get("rfq_id"):
        parsed["rfq_id"] = rfq.rfq_number
    return {
        "rfq_content": rfq.model_dump(exclude={"content"}),
        "parsed_rfq": parsed,
    }


# ---------------------------------------------------------------------------
# 2. validate_rules (LLM 사용 금지)
# ---------------------------------------------------------------------------
def _evidence_doc_type(issue: dict) -> Optional[str]:
    """계약 리스크는 영업 규정, 사양 이슈는 설계 표준. 물량·일정은 근거 문서가 없습니다."""
    if issue["type"] == "CONTRACT_RISK":
        return "policy"
    if issue["field"].startswith("spec."):
        return "design_std"
    return None


async def _attach_evidence(issue: dict, previous: dict[str, list]) -> None:
    if issue["field"] in previous:  # 재검증 때는 이전 근거 재사용
        issue["evidence"] = previous[issue["field"]]
    else:
        doc_type = _evidence_doc_type(issue)
        if doc_type:
            query = f"{rules.label(issue['field'])} {issue.get('reason') or ''} {issue.get('ref') or ''}".strip()
            issue["evidence"] = await _evidence(query, doc_type)
    if not issue.get("ref") and issue["evidence"]:
        issue["ref"] = issue["evidence"][0]["section"]


async def validate_rules(state: SG1State) -> dict:
    parsed = state["parsed_rfq"]
    assumptions = list(state.get("assumptions", []))
    issues, passed_checks, normalized = rules.validate(parsed, assumptions)

    previous = {i["field"]: i["evidence"] for i in state.get("issues", []) if i.get("evidence")}
    await asyncio.gather(*(_attach_evidence(i, previous) for i in issues))

    # 담당자 확인 없이 진행하는 경우: 누락 항목의 기본 가정값을 미승인 상태로 기록
    if not rules.has_unresolved_high(issues):
        assumed = {a["field"] for a in assumptions}
        for i in issues:
            if i["type"] == "MISSING" and i.get("proposed_value") and i["field"] not in assumed:
                assumptions.append(
                    Assumption(field=i["field"], value=i["proposed_value"], issue_id=i["id"]).model_dump()
                )
        normalized = rules.normalize_spec(parsed, assumptions)

    return {
        "issues": issues,
        "passed_checks": passed_checks,
        "normalized_spec": normalized,
        "assumptions": assumptions,
        "demand": _demand(parsed),
    }


def route_after_validate(state: SG1State) -> str:
    return "human_confirm" if rules.has_unresolved_high(state["issues"]) else "match_spec"


# ---------------------------------------------------------------------------
# 3. human_confirm
# ---------------------------------------------------------------------------
async def human_confirm(state: SG1State) -> dict:
    # 미해결 HIGH 전부 + 기본 가정값이 있는 미해결 누락 항목
    pending = [
        i for i in state["issues"]
        if not i.get("resolved")
        and (i["severity"] == "HIGH" or (i["type"] == "MISSING" and i.get("proposed_value")))
    ]

    # 멈춘 동안 화면에 이슈를 보여주기 위한 저장.
    # 재개 시 이 노드가 처음부터 다시 실행되어 한 번 더 호출되지만, 백엔드가 (rfq_number, thread_id)로 덮어씁니다.
    await mcp_tools.save_review(_review_payload(state, "WAITING"))

    decision = interrupt({
        "rfq_number": state["rfq_number"],
        "thread_id": state["thread_id"],
        "message": "확인이 필요한 항목이 있습니다. 제안값을 승인하거나 수정해 주세요.",
        "items": [
            {
                "issue_id": i["id"],
                "field": i["field"],
                "label": rules.label(i["field"]),
                "severity": i["severity"],
                "found": i["found"],
                "reason": i.get("reason"),
                "proposed_value": i.get("proposed_value"),
            }
            for i in pending
        ],
    }) or {}

    if not decision.get("approved"):
        logger.info("가정값 미승인: 재검증 후 다시 확인을 요청합니다.")
        return {}

    values: dict = dict(decision.get("values") or {})
    for i in pending:  # 수정값이 없는 항목은 제안값 그대로 승인
        if i["field"] not in values and i.get("proposed_value"):
            values[i["field"]] = i["proposed_value"]

    user_id = decision.get("user_id")
    issue_ids = {i["field"]: i["id"] for i in state["issues"]}
    assumptions = [a for a in state.get("assumptions", []) if a["field"] not in values]
    assumptions += [
        Assumption(field=f, value=str(v), issue_id=issue_ids.get(f), approved_by=user_id).model_dump()
        for f, v in values.items()
    ]
    return {
        "assumptions": assumptions,
        "confirmed_by": user_id,
        "confirmed_at": datetime.now().isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------------------
# 4. match_spec (판정은 코드, LLM은 설명 문장만)
# ---------------------------------------------------------------------------
EXPLAIN_SYSTEM = """당신은 자동차 부품사 설계 담당입니다.
주어진 사양 비교 결과(JSON)를 영업 담당자가 읽을 한국어 설명으로 3~5문장 작성합니다.
결과에 있는 판정과 숫자만 사용하고, 새 숫자나 판단을 추가하지 않습니다."""


async def match_spec(state: SG1State) -> dict:
    models = await mcp_tools.get_models()
    option_sets = await asyncio.gather(*(mcp_tools.get_options(m.model_id) for m in models))
    options_by_model = {o.model_id: [opt.model_dump() for opt in o.options] for o in option_sets}

    result = rules.match_models(
        state["normalized_spec"],
        [m.model_dump() for m in models],
        options_by_model,
        rules.get_path(state["parsed_rfq"], "commercial.target_price_krw"),
    )

    selected = result["selected_model"]
    design_query = "적합성 판정 원칙 표준 파생 사양 옵션 " + " ".join(result["options"])
    catalog_query = f"{selected} {' '.join(result['options'])}" if selected else "제품 개요"
    design_ev, catalog_ev = await asyncio.gather(
        _evidence(design_query, "design_std", k=3),
        _evidence(catalog_query, "catalog", k=3),
    )
    result["evidence"] = design_ev + catalog_ev

    explanation = await _llm().ainvoke([
        SystemMessage(EXPLAIN_SYSTEM),
        HumanMessage(json.dumps({k: v for k, v in result.items() if k != "evidence"}, ensure_ascii=False)),
    ])
    result["explanation"] = explanation.content

    return {
        "selected_model": selected,
        "options": result["options"],
        "fit_type": result["fit_type"],
        "match_result": result,
    }


def route_after_match(state: SG1State) -> str:
    return "build_report" if state["fit_type"] == "신규 개발" else "check_contract"


# ---------------------------------------------------------------------------
# 5. check_contract
# ---------------------------------------------------------------------------
async def check_contract(state: SG1State) -> dict:
    risks = []
    for i in state["issues"]:
        if i["type"] != "CONTRACT_RISK":
            continue
        evidence = i.get("evidence") or await _evidence(f"{rules.label(i['field'])} {i.get('ref') or ''}", "policy")
        risks.append(ContractRisk(
            item=f"{rules.label(i['field'])} {i['found']}",
            found=i["found"],
            limit=i.get("limit"),
            ref=i.get("ref"),
            suggestion=i.get("suggestion"),
            evidence=evidence,
        ).model_dump())
    return {"contract_risks": risks}


# ---------------------------------------------------------------------------
# 6. build_report
# ---------------------------------------------------------------------------
QUESTION_SYSTEM = """당신은 자동차 부품사 영업 담당입니다.
고객(완성차 구매팀)에게 보낼 RFQ 확인 질의서를 작성합니다.

규칙:
- 입력 이슈 하나당 질문 한 문장, 입력 순서 그대로 작성합니다.
- 정중한 한국어 문어체(~부탁드립니다, ~알려주십시오)로 씁니다.
- 원문 표기(found)와 차이를 질문에 포함합니다. 입력에 없는 숫자나 사실은 쓰지 않습니다.
- 우리 쪽 가정값이나 내부 판단은 쓰지 않습니다."""


async def build_report(state: SG1State) -> dict:
    targets = [i for i in state["issues"] if i["type"] in ("OUTLIER", "MISSING")]
    questions: list[str] = []
    if targets:
        writer = _llm().with_structured_output(CustomerQuestions, method="function_calling")
        brief = [
            {"항목": rules.label(i["field"]), "원문 표기": i["found"], "문제": i.get("reason")}
            for i in targets
        ]
        result: CustomerQuestions = await writer.ainvoke([
            SystemMessage(QUESTION_SYSTEM),
            HumanMessage(json.dumps(brief, ensure_ascii=False)),
        ])
        questions = result.questions

    contract_risks = state.get("contract_risks", [])
    demand = _demand(state["parsed_rfq"])
    saved = await mcp_tools.save_review(_review_payload(
        state,
        "DONE",
        selected_model=state.get("selected_model"),
        options=state.get("options", []),
        fit_type=state.get("fit_type"),
        customer_questions=questions,
        contract_risks=contract_risks,
    ))

    return {
        "review_id": saved.review_id,
        "customer_questions": questions,
        "contract_risks": contract_risks,
        "demand": demand,
    }
