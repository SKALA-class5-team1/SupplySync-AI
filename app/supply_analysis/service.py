"""SG2 실행과 공통 State/가이드 입력 사이의 경계."""

from typing import Any

from app.supply_analysis.schemas import SupplyAnalysisRequest, SupplyAnalysisResult


def normalize_input(state: dict[str, Any]) -> SupplyAnalysisRequest:
    """가이드의 평면 입력 또는 requirements/matched_product 공통 키를 받는다.

    공통 requirements에는 demand, contract_risks, rfq_number를,
    matched_product에는 selected_model(또는 model_id), options를 넣는다.
    양쪽 입력이 있으면 서로 같아야 한다. 모델/옵션을 SG2에서 재선택하지 않는다.
    """
    requirements = state.get("requirements") or {}
    matched = state.get("matched_product") or {}
    if not isinstance(requirements, dict) or not isinstance(matched, dict):
        raise ValueError("requirements와 matched_product는 객체여야 합니다")
    defaults = {
        "rfq_number": requirements.get("rfq_number"),
        "selected_model": matched.get("selected_model", matched.get("model_id")),
        "options": matched.get("options"),
        "demand": requirements.get("demand"),
        "contract_risks": requirements.get("contract_risks"),
        "base_date": requirements.get("base_date"),
    }
    data = {key: state[key] for key in SupplyAnalysisRequest.model_fields if key in state}
    for key, value in defaults.items():
        if value is not None:
            if key in data and data[key] != value:
                raise ValueError(f"SG1 평면 입력과 공통 State의 {key}가 일치하지 않습니다")
            data[key] = value
    return SupplyAnalysisRequest.model_validate(data)


async def analyze_supply(
    request: SupplyAnalysisRequest | dict[str, Any],
) -> SupplyAnalysisResult:
    from app.supply_analysis.graph import build_graph

    state = request.model_dump(mode="json") if isinstance(request, SupplyAnalysisRequest) else dict(request)
    result = await build_graph().compile().ainvoke(state)
    return SupplyAnalysisResult.model_validate(result)


async def supply_analysis(state: dict[str, Any]) -> dict[str, Any]:
    """상위 그래프 노드용. 입력과 내부 임시 키는 쓰지 않고 SG2 출력만 반환한다."""
    return (await analyze_supply(state)).model_dump(mode="json")


