"""SG2 공급 분석.

진입점
    POST /api/v1/supplysync/supply-analysis/analyze
    await service.supply_analysis(common_state)  # 상위 그래프용 출력 키만 반환
    graph.build_graph().compile()               # 직접 서브그래프로 연결

설정
    기존 core.config의 BUSINESS_MCP_URL, RAG_MCP_URL, INTERNAL_API_KEY 사용.
    공통 harness 도입 시 deps.gateway와 tracing.traced_node를 공통 구현으로 교체.
    RAG 도구의 실제 이름/문서 유형은 rag_search_docs / alt_parts.

계약
    bom/bom_effective는 비교·추적용 원 BOM이다.
    final_plan은 변경 없는 부품까지 포함하는 전체 최종 부품 ID → 필요 수량이다.
    SG3·SG4는 bom과 병합하지 않고 final_plan으로 최종 부품·수량을 확인한다.
    부분 대체는 원부품 잔여 수량과 대체품 수량을 담고, 전량 대체한 원부품은 제외한다.
    부족이 없으면 원 BOM 전체 수량을 final_plan에 담고 recommended_alt=None이다.
    State, selected_alternative.final_plan, SG2 저장 payload에 같은 전체 계획을 쓴다.
    보류·실패 시에는 final_plan={}이며 SG3로 넘기지 않는다.
    DB recommended_alt가 단일 enum이므로 전체 부족분에 같은 대안 종류를 적용한다.
    서로 다른 대안 종류를 혼합해야 하거나 전량 납기를 못 맞추면 hold.
    기존 공급사는 LOW, 신규 공급사 대체품은 VALIDATION_REQUIRED로 품질 위험을
    구분한다. DB에 정량 품질 지표가 생기면 이 마지막 비교 기준을 확장한다.
    근거 부족/알 수 없는 규칙/도구 실패는 hold이며 SG2 DONE으로 저장하지 않는다.
    지원하는 RAG 규정 문구가 바뀌면 rules.py와 테스트를 함께 갱신한다.

검증
    python -m pytest tests/supply_analysis -q
    LIVE_RAG=true python -m pytest tests/supply_analysis/test_rag_live.py -q
    실제 RAG 테스트도 Business 조회·저장은 테스트 대역을 사용한다.
"""
