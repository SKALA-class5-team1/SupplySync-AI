"""SG2 단독 실행 API. main.py의 기존 도메인 라우터 등록을 사용한다."""
from fastapi import APIRouter, status

from app.core.schemas import BaseResponse
from app.supply_analysis.schemas import SupplyAnalysisRequest, SupplyAnalysisResult
from app.supply_analysis.service import analyze_supply

router = APIRouter()

@router.post(
    "/analyze",
    tags=["Supply Analysis"],
    summary="SG2 공급 분석 및 검토 저장",
    response_model=BaseResponse[SupplyAnalysisResult],
    status_code=status.HTTP_200_OK,
    response_description="공급 계획과 next_step. 실패·미확정은 hold로 반환합니다.",
    responses={400: {"model": BaseResponse[None], "description": "요청 수량·날짜·필수 필드가 잘못되었습니다."}},
)
async def analyze(request: SupplyAnalysisRequest) -> BaseResponse[SupplyAnalysisResult]:
    """SG1이 정한 모델·옵션으로 BOM과 재고를 확인하고 SG2 결과를 저장합니다.

    인증은 현재 앱의 기존 도메인 API와 같이 별도 적용하지 않습니다.
    MCP 내부 인증에는 서버 설정의 INTERNAL_API_KEY를 사용합니다.
    base_date를 생략하면 MCP가 서울 기준 오늘을 사용합니다.
    200은 분석 결과를 반환했다는 의미이며, next_step=quote_drafting이고 saved=true일 때만
    다음 단계로 진행합니다. MCP 실패·RAG 근거 부족·납기 불충족은 hold입니다.
    """
    return BaseResponse.success(await analyze_supply(request))


