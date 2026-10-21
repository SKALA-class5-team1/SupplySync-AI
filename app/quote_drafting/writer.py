"""조건부 사항 문구 작성 (LLM은 문구만 쓴다)

LlmConditionWriter: 실제 LLM. TemplateConditionWriter: LLM 없이 쓰는 고정 문구 (로컬 개발·테스트용).
"""
import json
from typing import Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.core.config import settings
from app.quote_drafting.conditions import ConditionDraft

LLM_MODEL = "gpt-4.1-mini"

SYSTEM_PROMPT = """너는 자동차 부품사 대성오토텍의 견적 담당자다. 견적서 '조건부 사항' 각 항목의 문구(text)를 한 문장으로 쓴다.
규칙:
- 숫자는 그 항목의 facts·evidence에 있는 값만 쓴다. 계산하거나 새 숫자를 만들지 않는다.
- ASSUMPTION: 고객 사양이 비었거나 모호해 가정한 값을 밝힌다. rfq_value가 있으면 사양서 표기를 괄호로 덧붙인다.
- SUBSTITUTE_PART: 시작품 중 대체 부품을 적용하는 수량과 공급사를 밝힌다.
- CONTRACT_PROPOSAL: 고객 조건 대신 사내 한도(limit)로 수정을 제안한다. evidence에 단가 영향이 있으면 함께 밝힌다.
- 조항 번호는 ref 필드에 따로 저장되므로 문구에 쓰지 않는다. 내부 원가·마진은 쓰지 않는다.
- feedback이 있으면 그 문제를 고쳐서 다시 쓴다."""


class ConditionWriter(Protocol):
    async def write(self, drafts: list[ConditionDraft], feedback: list[str]) -> list[str]: ...


class _ConditionText(BaseModel):
    index: int = Field(description="입력 conditions의 index")
    text: str = Field(description="조건부 사항 문구 한 문장")


class _ConditionTexts(BaseModel):
    conditions: list[_ConditionText]


class LlmConditionWriter:
    def __init__(self, llm: BaseChatModel):
        self._llm = llm.with_structured_output(_ConditionTexts)

    async def write(self, drafts: list[ConditionDraft], feedback: list[str]) -> list[str]:
        payload = {
            "conditions": [
                {"index": i, "type": d.condition["type"], "facts": d.facts, "evidence": d.evidence}
                for i, d in enumerate(drafts)
            ],
            "feedback": feedback,
        }
        result = await self._llm.ainvoke(
            [SystemMessage(SYSTEM_PROMPT), HumanMessage(json.dumps(payload, ensure_ascii=False))]
        )
        texts = {c.index: c.text for c in result.conditions}
        # 빠진 항목은 빈 문구로 두고 FORMAT 검증에서 재작성시킨다
        return [texts.get(i, "") for i in range(len(drafts))]


def create_llm_writer() -> LlmConditionWriter:
    from langchain_openai import ChatOpenAI

    return LlmConditionWriter(ChatOpenAI(model=LLM_MODEL, api_key=settings.OPENAI_API_KEY, temperature=0))


_FIELD_LABELS = {"spec.flow": "유량", "spec.connector": "커넥터", "spec.coolant": "냉각수"}


class TemplateConditionWriter:
    async def write(self, drafts: list[ConditionDraft], feedback: list[str]) -> list[str]:
        return [self._text(d.condition["type"], d.facts) for d in drafts]

    @staticmethod
    def _text(kind: str, f: dict) -> str:
        if kind == "ASSUMPTION":
            label = _FIELD_LABELS.get(f["field"], f["field"])
            text = f"{label}: {f['assumed_value']}(으)로 가정"
            return f"{text} (사양서 표기 {f['rfq_value']})" if f["rfq_value"] else text
        if kind == "SUBSTITUTE_PART":
            return (
                f"시작품 {f['sample_qty']:,}개 중 {f['qty']:,}개에 대체 부품 {f['substitute_part_id']}"
                f"({f['substitute_supplier']}) 적용, 추가 비용 {f['extra_cost_krw']:,}원은 단가와 별도"
            )
        return f"{f['customer_terms']} 조건을 {f['limit']}(으)로 수정 제안"
