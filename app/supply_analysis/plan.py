"""SG2 → SG3 인계 시 전체 부품 계획의 수량 보존을 확인한다."""

from collections import Counter

from app.supply_analysis.schemas import SupplyAnalysisResult


def validate_final_plan(result: SupplyAnalysisResult) -> None:
    """delta 계획이나 미선택 후보가 견적·재고 재확인으로 넘어가지 않게 한다."""
    if not result.saved or result.next_step != "quote_drafting" or not result.final_plan:
        raise ValueError("저장된 SG2 전체 공급 계획이 필요합니다")
    original = {part.part_id: part.required_qty for part in result.bom}
    if not original or len(original) != len(result.bom):
        raise ValueError("SG2 원 BOM이 비어 있거나 중복 부품이 있습니다")
    expected = Counter(original)
    shortages = {part.part: part for part in result.shortages}
    if len(shortages) != len(result.shortages) or any(
        pid not in original or part.needed != original[pid]
        or part.short_qty != max(0, part.needed - part.available)
        for pid, part in shortages.items()
    ):
        raise ValueError("부족 부품·수량이 원 BOM과 일치하지 않습니다")
    selected = result.selected_alternative
    if shortages:
        if selected is None or selected.type != result.recommended_alt or not selected.meets_due_date:
            raise ValueError("납기를 충족하는 선택 대안이 필요합니다")
        if len(selected.parts) != len(shortages) or {a.part_id for a in selected.parts} != set(shortages):
            raise ValueError("선택 대안이 전체 부족 부품을 포함하지 않습니다")
        for alternative in selected.parts:
            if not alternative.feasible or not alternative.meets_due_date or alternative.type != selected.type:
                raise ValueError("실행 불가능한 대안이 공급 계획에 포함되어 있습니다")
            if alternative.type == "SUBSTITUTE_PART":
                substitute = alternative.substitute_part_id
                if not substitute or substitute == alternative.part_id:
                    raise ValueError("대체 부품 ID가 올바르지 않습니다")
                qty = shortages[alternative.part_id].short_qty
                expected[alternative.part_id] -= qty
                expected[substitute] += qty
        if selected.final_plan != result.final_plan:
            raise ValueError("선택 대안과 전체 final_plan이 다릅니다")
    elif selected is not None or result.recommended_alt is not None:
        raise ValueError("부족분 없이 대체 계획이 지정되어 있습니다")
    if result.final_plan != {pid: qty for pid, qty in expected.items() if qty > 0}:
        raise ValueError("final_plan에 변경 없는 부품이 누락되었거나 대체 수량이 다릅니다")
