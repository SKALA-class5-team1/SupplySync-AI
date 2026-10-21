# SupplySync AI

## 로컬 실행 설정

설정은 `app/core/config.py` 한 파일에서 관리하고, 프로젝트 루트의 `.env`를 읽습니다.
`env.example`을 `.env`로 복사한 뒤 필요한 값을 수정하세요. 별도 환경 파일이나 `ENV` 선택은 사용하지 않으며 실행 환경은 항상 `local`입니다.

```bash
cp env.example .env
.venv/bin/python main.py
```

프로젝트 가상환경에 `requirements.txt`의 패키지가 설치되어 있어야 합니다.
기본 주소는 `http://127.0.0.1:8000`, Swagger UI는 `/docs`입니다.
설정 우선순위는 환경 변수 > `.env` > 기본값입니다. `DEBUG`와 `RELOAD`는 `true`/`false`, `CORS_ORIGINS`는 JSON 배열로 작성하세요.
로그는 콘솔에만 출력하며 MCP 서버에 연결하지 않아도 AI 서버 자체를 시작할 수 있습니다.

## 로컬 인프라 연결 설정

접속 설정은 `env.example`에 정리되어 있습니다. Business MCP는 아래 "business-mcp 연결"대로 연결되어 있고,
RAG MCP는 각 SG의 도구 모듈에서 `rag_search_docs`를 호출합니다. RabbitMQ는 접속 설정만 준비된 상태입니다.

| 대상 | 설정 | 사용 방식 |
| --- | --- | --- |
| FastAPI | `HOST`, `PORT` | 기본 `127.0.0.1:8000`에서 AI 서버 실행 |
| Business MCP | `BUSINESS_MCP_URL` | `http://127.0.0.1:8101/mcp` |
| RAG MCP | `RAG_MCP_URL` | `http://127.0.0.1:8102/mcp` |
| RAG API 문서 | `RAG_API_DOCS_URL` | `http://127.0.0.1:8102/docs` (문서 확인용) |
| RabbitMQ | `RABBITMQ_HOST`, `RABBITMQ_AMQP_PORT`, `RABBITMQ_USER`, `RABBITMQ_PASSWORD` | 접속 설정만 준비. 발행·소비 코드는 큐와 메시지 규격 확정 후 구현 |
| Spring Boot (선택) | `SERVER_PORT`, `INTERNAL_API_KEY` | 내부 HTTP API 유틸리티용. `INTERNAL_API_KEY`는 business-mcp 인증에도 같이 씀 |

Spring Boot 내부 API를 직접 호출해야 할 때는 `app.core.http_client`의 `get_internal_api_client()`를 `async with`로 열고,
`client.get("/internal/...")`처럼 실제 백엔드 경로를 전달합니다. `X-Internal-Api-Key`가 자동으로 설정되며 사용 후 클라이언트가 닫힙니다.
이 클라이언트는 설정된 백엔드의 `/internal/**` 경로만 허용하고 리다이렉트는 자동으로 따라가지 않습니다.

## business-mcp 연결

`app/core/mcp_client.py`가 business-mcp(`BUSINESS_MCP_URL`)에 MCP로 연결합니다.
처음 쓰는 경우 **[MCP_GUIDE.md](MCP_GUIDE.md)** 를 먼저 보세요 (준비, 사용법, Tool 목록, 오류 처리, 자주 겪는 문제).
요청 헤더 `X-Internal-Api-Key`에는 `.env`의 `INTERNAL_API_KEY`를 씁니다. business-mcp·백엔드의 `INTERNAL_API_KEY`와 같은 값이어야 합니다.

| 함수 | 용도 |
| --- | --- |
| `get_business_tools()` | LLM에 `bind_tools`로 넘길 Tool. 이름에 `business_` 접두어 (예: `business_get_bom`) |
| `call_business_tool("get_bom", {...})` | 코드에서 직접 호출. 결과 JSON을 dict로 반환하고, Tool 오류면 `McpToolError` |

연결 확인 (Spring 백엔드(demo)와 business-mcp 서버를 먼저 실행):

```bash
LIVE_MCP=true .venv/bin/python -m pytest tests/core/test_business_mcp_live.py -v
```

## SG1~SG4 연결

`app/supervisor/graph.py`가 네 서브그래프를 동일한 `thread_id`와 체크포인터로 실행합니다.

| 단계 | 입력과 다음 단계 전달 |
| --- | --- |
| SG1 RFQ 검토 | RFQ 번호로 원문을 조회하고 `parsed_rfq`, `selected_model`, `options`, `demand`, 가정·계약 리스크를 전달 |
| SG2 공급 분석 | 모델·옵션·시작품 수량으로 BOM을 조회. 원본 `bom`과 `bom_effective`를 보존하고 **전체 부품 수량**을 `final_plan`에 저장 |
| SG3 견적 작성 | `final_plan`과 SG2 결과의 일치·전체 수량을 검증. 실제 선택된 모든 대체 부품을 견적 조건으로 작성하고 저장 승인 후 초안 저장 |
| SG4 승인·등록 | 최종 승인 후 **동일한 전체 `final_plan`**으로 재고 재조회. 수량·응답이 일치하고 재고가 충분할 때 등록 |

`final_plan`은 변경 없는 부품도 포함합니다. 예를 들어 원래 제어칩 2,000개 중 500개만 교체했다면
원 제어칩 1,500개, 대체 칩 500개와 나머지 모든 부품이 들어갑니다. 전량 교체한 원 부품은 제외합니다.
SG3·SG4에서 원 BOM과 다시 합치지 않습니다. SG3의 `supply_analysis`는 SG2 결과와 `demand`의 스냅샷이며,
같은 `final_plan`을 담습니다. 정책 제21조에 따라 양산 기본 단가는 모델·옵션 기준으로 계산하고,
시작품 대체 부품의 추가 비용은 별도 견적 조건으로 전달합니다.

분기는 다음과 같습니다.

- SG1 모델 선정 실패, SG2 공급 대안 없음·저장 실패, SG3 저장 실패·응답 불명: `ON_HOLD`. 다음 단계로 진행하지 않습니다.
- SG4 `REVISE`: 견적 차수를 올려 SG3 재작성 → 저장 승인 → 최종 승인을 다시 받습니다.
- SG4 재고 부족: SG2 재분석 → 새 차수의 SG3 견적 → 두 승인을 다시 받습니다. 잘못된 재고 응답은 보류합니다.
- SG4 `REJECTED`: 최종 반려로 종료합니다. `REGISTERED`면 `DONE`입니다.
- 재작성은 기존 SG3 규칙에 따라 최대 4차까지입니다. 한도 초과는 `ON_HOLD`입니다.

### 백엔드 호출 API

공통 접두어는 `/api/v1/supplysync`입니다. 모든 요청에 `X-Internal-Api-Key` 헤더가 필요합니다.
사용자 권한 확인과 승인 기록은 백엔드가 처리한 후 재개 API를 호출합니다.

| 메서드·경로 | 동작 |
| --- | --- |
| `POST /jobs` | 첫 담당자 입력 대기 또는 종료까지 실행 |
| `GET /jobs/{thread_id}` | 진행 상태, 공유 State, 현재 대기 요청 조회 |
| `POST /jobs/{thread_id}/resume` | 현재 대기 요청에 응답하고 다음 대기 또는 종료까지 실행 |

시작 본문:

```json
{"thread_id": "th_demo", "rfq_number": "HB-EV7-EWP-001"}
```

재현 테스트에는 선택 필드 `base_date`, `issue_date`를 `YYYY-MM-DD`로 지정할 수 있습니다.
`base_date`를 생략하면 SG2와 SG4가 각각 조회 시점의 서버 날짜를 사용합니다.
응답은 `BaseResponse` 형식이며 `result`에 `status`, `current_stage`, `next_step`, `waiting_for`, `state`가 있습니다.
승인 대기 시 `status=WAITING`이고, `waiting_for`는 `{id, payload}`입니다.

SG3 저장 승인 또는 SG4 최종 승인 재개 본문 (`step`은 현재 `waiting_for.payload.step` 그대로 사용):

```json
{
  "interrupt_id": "현재 waiting_for.id",
  "response": {
    "step": "SG3.request_save_approval",
    "userId": 2,
    "decision": {"result": "APPROVED"}
  }
}
```

SG4의 `step`은 `SG4.request_approval`입니다. `REVISE`에는 `reason`과 선택적인 `changes`를
`decision` 안에 넣습니다. `REJECTED`는 SG4에서만 허용되며 `reason`이 필요합니다.
SG1 확인 재개는 동일한 바깥 형식에 아래 `response`를 사용합니다.

```json
{"step": "SG1.human_confirm", "approved": true, "user_id": 2, "values": {"spec.flow": "22 L/min"}}
```

중복 시작, 이전 `interrupt_id`, 다른 `step`, 종료 후 재개는 `409`입니다. 요청 형식 오류는 `400`, 승인 본문 오류는 `422`,
없는 실행은 `404`, 인증 실패는 `401`입니다. 잘못된 입력은 대기 상태를 소비하지 않습니다.
SG1 대기 저장과 interrupt를 분리하여 재개할 때 검토 저장이 중복 실행되지 않습니다.

기본 체크포인터는 **프로세스 메모리**입니다. 로컬 단일 워커 실행용으로 서버 재시작 시 상태가 사라집니다.
운영 시 지속성 체크포인터를 `build_graph(checkpointer=...)`에 주입하고, 여러 워커 사이의 실행 잠금도
공유해야 합니다. 현재 API는 다음 대기까지 기다리는 HTTP 방식이며 RabbitMQ·SSE는 연결하지 않았습니다.

### 연결 검증

```bash
DEBUG=false .venv/bin/python -m pytest -q
```

`tests/workflow`는 MCP·LLM 응답만 대체하고 실제 네 그래프와 체크포인터를 실행합니다.
정상 등록, SG1 확인값 반영, 전체 BOM 전달, 공급 보류, 저장 응답 유실, 수정 요청,
재고 변경에 따른 재분석, 재작성 한도, 잘못된 재고 응답, 중복 승인, API 시작·조회·재개를 검증합니다.
실제 업무 실행에는 Business MCP·RAG MCP·백엔드와 LLM 설정이 필요합니다.

## Swagger 문서 작성 기준

서버 실행 후 `/docs`에서 API 문서를 확인하고 **Try it out**으로 요청할 수 있습니다.
`main.py`의 `/`, `/health`와 `app/core/schemas.py`의 응답 모델을 작성 예시로 참고하세요.

| 경로 | 용도 | 성공 응답 |
| --- | --- | --- |
| `GET /` | 서비스 이름, 실행 환경, API 버전 확인 | `200`, `ServiceInfoResponse` |
| `GET /health` | HTTP 요청에 응답 가능한지 확인하는 생존 검사 | `200`, `HealthCheckResponse` |

새 API를 작성할 때는 다음 순서를 따릅니다.

1. 도메인의 `schemas.py`에 구체적인 요청·응답 모델을 정의하고, 각 `Field`에 `description`과 `examples`를 작성합니다.
2. 라우트 데코레이터에 `tags`, `summary`, `response_model`, `status_code`, `response_description`을 명시합니다.
3. 함수 docstring에 사용 목적, 인증 여부, 입력값, 성공 응답의 의미와 검사 범위를 설명합니다. 이 내용은 Swagger의 API 설명으로 표시됩니다.
4. 반환 타입과 실제 반환값을 응답 모델에 맞춥니다. 위 시스템 API는 기존 JSON 형식을 유지하며, 비즈니스 API에는 공통 형식인 `BaseResponse[구체적인응답모델]`을 사용합니다.
5. 실패 응답이 있는 API는 실제 동작하는 상태 코드·모델·예시를 `responses`에 추가합니다. 인증이 없는 API에 `401`을 넣는 등 실제 동작과 다른 문서는 작성하지 않습니다.
6. `/docs`의 **Example Value**, **Schema**, **Try it out** 결과가 실제 응답과 일치하는지 확인합니다.
7. 예시와 같이 너무 자세하게 작성하지는 않아도 됩니다.
