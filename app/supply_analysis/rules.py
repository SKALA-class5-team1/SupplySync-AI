"""RAG 원문에서 지원하는 규칙을 읽는다. 알 수 없는 규칙을 추측하지 않는다.

현재 승인된 alt_parts_criteria 1~3절과 영업 규정 13·20·21조를 지원한다.
새 문구/규칙은 이 파서를 확장하고 테스트한 뒤 적용해야 한다. 문서는 실행마다
MCP에서 받고, 청크 ID만으로 성능 등급이나 추천 순서를 추정하지 않는다.
"""

import re
from dataclasses import dataclass

from app.supply_analysis.schemas import Criterion, DocHit, Evidence, Substitute


class CriteriaError(ValueError):
    """RAG 근거가 없거나 현재 판정 코드가 지원하지 않는 규칙."""


def normalize(text: str) -> str:
    return " ".join(text.split())


def evidence(docs: list[DocHit]) -> list[Evidence]:
    return [
        Evidence(chunk_id=d.chunk_id, section=d.section, excerpt=d.content)
        for d in {d.chunk_id: d for d in docs}.values()
    ]


@dataclass(frozen=True)
class Criteria:
    order: list[Criterion]
    comparison: DocHit
    eligibility: DocHit | None


def read_criteria(docs: list[DocHit]) -> Criteria:
    labels: dict[str, Criterion] = {
        "납기 충족 여부": "meets_due_date",
        "추가 원가": "extra_cost_krw",
        "검증 기간": "validation_days",
        "품질 위험": "quality_risk",
    }
    comparisons = []
    eligibility = None
    condition = (
        "핵심 부품의 가용 재고가 필요 수량보다 적고, 입고 예정일이 고객 납기보다 늦으면 "
        "대체 부품을 검토한다. 대체 부품은 원래 부품과 같은 성능 등급 이상이어야 한다."
    )
    for doc in docs:
        content = normalize(doc.content)
        if content == condition:
            eligibility = doc
        match = re.fullmatch(
            r"대안은 (.+) 순으로 비교한다\. "
            r"납기를 맞출 수 없는 대안은 다른 조건이 좋아도 우선순위에서 밀린다\.",
            content,
        )
        if match:
            terms = [term.strip() for term in match[1].split(",")]
            if len(terms) == 4 and set(terms) == set(labels):
                order = [labels[term] for term in terms]
                if order[0] != "meets_due_date":
                    raise CriteriaError("RAG 비교 순서와 납기 우선 규칙이 충돌합니다")
                comparisons.append((order, doc))
    if not comparisons:
        raise CriteriaError("RAG에서 지원하는 대안 비교 기준(3절)을 확인하지 못했습니다")
    if any(order != comparisons[0][0] for order, _ in comparisons):
        raise CriteriaError("RAG 대안 비교 기준이 서로 충돌합니다")
    return Criteria(comparisons[0][0], comparisons[0][1], eligibility)


def substitute_rule(
    part_id: str, candidate: Substitute, docs: list[DocHit]
) -> tuple[int, DocHit]:
    """원 부품→후보 관계·동일 등급·핀 배치·검증 기간을 원문으로 확인한다.

    승인된 2절 형식 전체를 검증한다. 단순히 '같은 등급' 키워드만 있거나
    ref_chunk_id만 일치하는 다른/개정 문서를 승인 근거로 쓰지 않는다.
    원가의 실제 값은 Business MCP를 사용한다.
    """
    doc = next((d for d in docs if d.chunk_id == candidate.ref_chunk_id), None)
    if doc is None:
        raise CriteriaError(f"{candidate.substitute_part_id}: 참조 청크를 찾지 못했습니다")
    pattern = (
        r"[^.]+\(" + re.escape(part_id) + r"\)의 대체품은 "
        + re.escape(candidate.supplier) + r"의 "
        + re.escape(candidate.substitute_part_id)
        + r"다\. 같은 [^.]+ 등급이며 핀 배치가 같아 회로 변경이 필요 없다\. "
        r"다만 처음 쓰는 공급사이므로 적용 전 (\d+)(주|일)간의 검증 시험이 필요하고, "
        r"개당 원가가 [\d,]+원 높다\."
    )
    match = re.fullmatch(pattern, normalize(doc.content))
    if not match:
        raise CriteriaError(f"{candidate.substitute_part_id}: 성능 등급·호환성·검증 규칙을 확인할 수 없습니다")
    days = int(match[1]) * (7 if match[2] == "주" else 1)
    if days <= 0:
        raise CriteriaError("RAG 검증 기간은 0보다 커야 합니다")
    return days, doc


def purchase_policies(docs: list[DocHit]) -> list[DocHit]:
    texts = [
        "처음 거래하는 공급사의 부품은 검증 시험을 통과한 뒤에만 생산에 투입한다. "
        "대체 부품을 사용하는 경우 견적 조건에 그 사실을 명시해 고객에게 알린다.",
        "대체 부품 사용으로 부품 원가가 오르는 경우, 증가분은 견적 단가에 포함하지 않고 "
        "견적 조건에 별도 비용으로 표시한다.",
    ]
    found = [next((d for d in docs if normalize(d.content) == text), None) for text in texts]
    if any(doc is None for doc in found):
        raise CriteriaError("RAG 구매 규정 제20·21조를 확인하지 못했습니다")
    return found


def penalty_policy(docs: list[DocHit]) -> DocHit | None:
    text = (
        "납기 지연에 대한 배상 책임은 해당 계약 금액의 10%를 상한으로 한다. "
        "배상 상한이 없거나 10%를 초과하는 조항은 수용할 수 없으며, 고객과 상한 설정을 협의해야 한다."
    )
    return next((d for d in docs if normalize(d.content) == text), None)
