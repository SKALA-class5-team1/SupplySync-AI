"""
실제 SG1·SG2·SG3 를 상위 그래프 노드로 불러오는 곳. SG 코드는 고치지 않고 가져다 쓰기만 한다.

- 각 SG 는 아직 main 에 다 합쳐지지 않았다. import 가 안 되면 그 자리는 가짜 노드로 남긴다(경고 로그).
- SG1/SG3 는 서브그래프(compile 해서 노드로), SG2 는 BOM 조회 -> 어댑터 -> 분석 서비스 호출 노드로 붙인다.
"""
import logging
from typing import Any, Awaitable, Callable, Optional

from app.supervisor import adapters

logger = logging.getLogger(__name__)

BomLoader = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


async def default_bom_loader(state: dict[str, Any]) -> dict[str, Any]:
    """Business MCP get_bom: 선택 모델 + 옵션 + 시작품 수량의 BOM (MCP_GUIDE.md SG2 예시)."""
    from app.core.mcp_client import call_business_tool

    demand = state.get("demand") or {}
    return await call_business_tool(
        "get_bom",
        {
            "model_id": state.get("selected_model"),
            "options": state.get("options") or [],
            "qty": demand.get("sample_qty"),
        },
    )


def load_sg1() -> Any:
    """SG1 서브그래프. checkpointer=None 이면 상위 그래프의 checkpointer 를 이어받는다."""
    from app.rfq_review.graph import build_graph

    return build_graph(checkpointer=None)


def load_sg3() -> Any:
    """SG3 서브그래프 (build_graph 는 컴파일 전 StateGraph 를 돌려준다)."""
    from app.quote_drafting.graph import build_graph

    return build_graph().compile()


def make_sg2_node(bom_loader: Optional[BomLoader] = None) -> Callable[[Any], Awaitable[dict[str, Any]]]:
    """SG2 노드: SG1 출력으로 BOM 을 가져오고 -> SG2 입력으로 바꿔 분석 -> SG3·SG4 입력으로 바꿔 돌려준다."""
    from app.supply_analysis.schemas import SupplyAnalysisRequest
    from app.supply_analysis.service import analyze_supply

    loader = bom_loader or default_bom_loader

    async def sg2_supply_analysis(state: Any) -> dict[str, Any]:
        data = state.model_dump()
        bom = await loader(data)
        request = SupplyAnalysisRequest.model_validate(adapters.sg1_to_sg2_request(data, bom))
        result = await analyze_supply(request)
        return adapters.sg2_result_to_state(result.model_dump(mode="json"), data, bom)

    return sg2_supply_analysis


def load_real_nodes(bom_loader: Optional[BomLoader] = None) -> dict[str, Any]:
    """불러올 수 있는 실제 노드만 {sg1, sg2, sg3} 로 돌려준다. 못 불러온 것은 빠진다(가짜 유지)."""
    nodes: dict[str, Any] = {}
    for name, factory in (
        ("sg1", load_sg1),
        ("sg2", lambda: make_sg2_node(bom_loader)),
        ("sg3", load_sg3),
    ):
        try:
            nodes[name] = factory()
        except ImportError as exc:
            logger.warning("%s 를 불러오지 못해 임시 노드를 씁니다: %s", name.upper(), exc)
    return nodes
