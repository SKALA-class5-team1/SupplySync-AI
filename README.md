# SupplySync AI

## 로컬 실행 설정

설정은 `app/core/config.py` 한 파일에서 관리하고, 프로젝트 루트의 `.env`를 읽습니다.
`env.example`을 `.env`로 복사한 뒤 필요한 값을 수정하세요. 별도 환경 파일이나 `ENV` 선택은 사용하지 않으며 실행 환경은 항상 `local`입니다.

```bash
cp env.example .env
RELOAD=false .venv/bin/python main.py
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
| Spring Boot | `SERVER_PORT`, `INTERNAL_API_KEY` | `http://localhost:{SERVER_PORT}/internal/tasks/{task_id}/events`로 진행 이벤트 전송. 키는 business-mcp 인증에도 사용 |

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
| SG3 견적 작성 | `final_plan`과 SG2 결과의 일치·전체 수량을 검증. 실제 선택된 모든 대체 부품을 견적 조건으로 작성하고 `POST /internal/quotes`로 즉시 저장. 저장 승인·초안 PUT 없음 |
| SG4 승인·등록 | 최종 승인 후 **동일한 전체 `final_plan`**으로 재고 재조회. 수량·응답이 일치하고 재고가 충분할 때 등록 |

`final_plan`은 변경 없는 부품도 포함합니다. 예를 들어 원래 제어칩 2,000개 중 500개만 교체했다면
원 제어칩 1,500개, 대체 칩 500개와 나머지 모든 부품이 들어갑니다. 전량 교체한 원 부품은 제외합니다.
SG3·SG4에서 원 BOM과 다시 합치지 않습니다. SG3의 `supply_analysis`는 SG2 결과와 `demand`의 스냅샷이며,
같은 `final_plan`을 담습니다. 정책 제21조에 따라 양산 기본 단가는 모델·옵션 기준으로 계산하고,
시작품 대체 부품의 추가 비용은 별도 견적 조건으로 전달합니다.

분기는 다음과 같습니다.

- SG1 HIGH 이슈: 체크포인트 저장 → `CLARIFICATION_REQUIRED` → 확인값과 `userId`를 반영해 재검토합니다.
- SG1 모델 선정 불가, SG2 사용 가능한 공급 대안·검토 기준 없음: `RUN_ON_HOLD`. SG2는 먼저 검토 결과에 `final_plan={}`을 저장합니다.
- 도구·모델·응답 검증·저장 실패: `RUN_FAILED` 이벤트로 원인을 전달하고 다음 단계로 진행하지 않습니다.
- SG3는 처음 `revision=1`로 저장하고 SG4 승인 대기로 갑니다. 별도의 저장 승인 단계가 없습니다.
- SG4 `APPROVED`: 전체 부품 재고 확인 → `POST /internal/quotes/{quote_id}/register` 성공 확인 → `RUN_END`의 `data.outcome=REGISTERED`.
- SG4 `REJECTED`: `RUN_END`의 `data.outcome=REJECTED`. 추가 `DONE` 이벤트는 보내지 않습니다.
- SG4 `REVISE`: `RUN_END` 없이 차수 +1 → SG3 재작성·저장 → SG4 승인. 재고 부족도 SG2 재분석을 거쳐 차수 +1로 저장합니다.
- 처음 1차 + 수정 최대 3회, 총 4차까지입니다. 한도에 도달하면 `RUN_ON_HOLD`입니다.

### 백엔드 호출 API

BE와 같은 호스트에서 실행할 때 **`AGENT_BASE_URL=http://127.0.0.1:8000`** 입니다.
다른 호스트에서 접근해야 하면 `HOST=0.0.0.0`으로 실행하고 BE에는 실제 AI 서버의 IP 또는 도메인과 포트 `8000`을 설정합니다.
`0.0.0.0` 자체는 BE의 접속 주소로 사용하지 않습니다.

아래 경로는 **루트 경로**이며 `/api/v1/supplysync` 접두어를 붙이지 않습니다.
모든 호출에 BE와 동일한 `X-Internal-Api-Key`가 필요합니다. 사용자 권한 확인·승인 기록은 BE가 처리합니다.

| 메서드·경로 | 동작 |
| --- | --- |
| `POST /runs` | 즉시 `202`로 접수하고 백그라운드 실행 |
| `GET /runs/{thread_id}` | 진행 상태, 공유 State, 현재 대기 요청 조회 |
| `POST /runs/{thread_id}/resume` | 현재 대기 입력 검증 후 즉시 `202`로 접수하고 백그라운드 재개 |

시작 본문:

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "rfq_id": 1,
  "rfq_number": "HB-EV7-EWP-001",
  "thread_id": "task-550e8400-e29b-41d4-a716-446655440000",
  "request_id": "be-request-001"
}
```

`thread_id`는 BE가 준 값을 그대로 LangGraph에서 사용합니다. 재현 테스트에는 선택 필드 `base_date`,
`issue_date`를 `YYYY-MM-DD`로 지정할 수 있습니다. `base_date` 생략 시 SG2와 SG4가 각각 조회 시점의 날짜를 사용합니다.

시작과 재개는 그래프·외부 API 응답을 기다리지 않고 `202 Accepted`를 반환합니다.
접수 응답은 명령을 받았다는 뜻이며 성공·완료를 의미하지 않습니다.
백그라운드에서 입력 대기·업무 보류·완료·실패에 도달하면 BE callback으로 알립니다.
기존 `RUN_TIMEOUT_SEC=270`은 HTTP 대기 시간과 별개인 백그라운드 명령 1건의 실행 한도입니다.
초과 시 작업을 중단하고 `RUN_FAILED`에 실패 사유를 보냅니다. 현재 실행 한도는 BE의 30분 무이벤트 감시 한도보다 짧습니다.

같은 `request_id`와 같은 본문의 재요청은 작업을 다시 시작하지 않습니다.
실행 중·완료·실패 여부와 관계없이 최초와 동일한 접수 정보로 `202`를 반환합니다.
동일 식별자에 다른 실행 본문을 쓰면 `409`입니다. 현재 상태는 `GET /runs/{thread_id}`에서 조회합니다.

접수 응답은 `BaseResponse` 형식이며 `result`에는 아래 값만 담습니다.

```json
{"task_id": "550e8400-e29b-41d4-a716-446655440000", "request_id": "be-request-001",
 "thread_id": "task-550e8400-e29b-41d4-a716-446655440000", "status": "ACCEPTED"}
```

`GET /runs/{thread_id}`의 `result`에는 식별자와 현재 `status`, `current_stage`,
`next_step`, `waiting_for`, `state`가 있습니다. 대기 시 `status=WAITING`이며
`waiting_for={id, payload}`입니다. BE가 내부 `interrupt_id`를 재개 본문에 보낼 필요는 없습니다.

요구 확인 재개:

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "thread_id": "task-550e8400-e29b-41d4-a716-446655440000",
  "kind": "CLARIFICATION",
  "userId": 2,
  "decision": {"assumptions": [{"field": "spec.flow", "value": "22 L/min"}]}
}
```

SG4 승인·반려·수정 재개:

```json
{
  "step": "SG4.request_approval",
  "userId": 2,
  "decision": {"result": "APPROVED", "reason": null, "changes": {}}
}
```

`REJECTED`·`REVISE`에는 `reason`이 필요합니다. 선택 필드 `quoteId`를 보내면 현재 견적과 대조합니다.
생략한 요청은 현재 대기 견적에 적용되므로 이전 차수의 지연 응답까지 식별하려면 BE가 `quoteId`를 함께 보내야 합니다.
재개 접수 시 내부 대기 ID를 선점하여 동시에 도착한 같은 대기의 두 번째 요청은 `409`로 거절합니다.
접수 처리는 그래프 실행 잠금을 기다리지 않습니다. 대기 이벤트 직후 들어온 재개도 먼저 접수한 뒤
같은 thread의 이전 실행이 끝나면 이어서 실행합니다. 재개가 접수된 대기는 GET에서 다시 노출하지 않습니다.
요청 형식 오류는 `400`, 승인 내용 오류는 `422`, 없는 실행은 `404`, 인증 실패는 `401`,
단계·식별자 불일치 또는 종료 후 재개는 `409`, 서버 종료 중 새 명령은 `503`입니다.
잘못된 입력은 체크포인트를 소비하지 않습니다. 접수 이후 오류는 HTTP 대신 `RUN_FAILED` 이벤트로 전달합니다.

### 진행 이벤트와 시연 설정

`POST {BE}/internal/tasks/{task_id}/events`에 내부 인증 헤더와 다음 형식으로 보냅니다.

```json
{"type": "STEP_START", "step": "SUPPLY_CHECK", "message": "부품 공급을 확인합니다"}
```

`SUPPLY_CHECK`·`ALTERNATIVE_ANALYSIS`는 이벤트의 `step`입니다. 후자는 부족분이 있을 때만 발생합니다.
첫 이벤트는 접수 직후 `STEP_START / RFQ_ANALYSIS`이며, 단계가 바뀌거나 처리가 끝나면 `STEP_END`를 보냅니다.
단계는 `RFQ_ANALYSIS`, `SPEC_MATCH`, `SUPPLY_CHECK`, `ALTERNATIVE_ANALYSIS`, `QUOTE`,
`FINAL_REVIEW`, `APPROVAL`, `REVALIDATION`, `REGISTRATION`입니다.
LangChain 도구 호출 callback에서는 `TOOL_START/TOOL_END`를 전송합니다. 도구 입력·결과 원문은 이벤트에 싣지 않습니다.
`CLARIFICATION_REQUIRED`의 step은 `SPEC_MATCH`, `APPROVAL_REQUIRED`의 step은 `APPROVAL`입니다. 두 대기 이벤트는
그래프 실행이 interrupt에서 반환되고 체크포인트를 읽은 뒤 전송합니다. 이벤트의 `data`에 확인 항목·승인 카드와 실행 ID를 포함합니다.

전송은 1회 최대 3초, 일시 오류(연결 실패·시간 초과·408·429·5xx)에 최대 2회 재시도합니다.
BE의 `409`는 이미 종료된 작업으로 보고 무시합니다. 다른 전송 실패는 실행 실패로 처리하며
`RUN_FAILED` 전송도 같은 규칙으로 재시도합니다. BE에 전혀 연결되지 않으면 전송 실패를 로그에 남기며
`GET /runs/{thread_id}`의 `status=FAILED`, `state.error`에서 원인을 확인할 수 있습니다.
견적 저장·등록 같은 업무 쓰기는 이 이벤트 재시도와 별개로 자동 재실행하지 않습니다.

체크포인터·접수 기록·잠금은 **프로세스 메모리**에 있습니다. 시연은 단일 워커로 실행하고
`RELOAD=false .venv/bin/python main.py`를 사용해 자동 재시작을 끕니다. 서버를 재시작하면 대기 상태와 중복 접수 기록이 사라집니다.
서버는 백그라운드 Task 참조를 유지하고, 정상 종료 시 이미 접수한 실행과 이벤트 전송이 끝날 때까지 기다립니다.

### 연결 검증

```bash
DEBUG=false .venv/bin/python -m pytest -q
```

`tests/workflow`는 MCP·LLM·BE HTTP 응답을 대체하고 실제 네 그래프와 체크포인터를 실행합니다.
정상 등록, 확인자 기록, 전체 BOM 전달, 빈 계획 저장 후 업무 보류, 기술 오류, 수정 한도,
재고 변경·새 revision, 즉시 재개, 중복 요청, 이벤트 재시도·409, 루트 API 인증을 검증합니다.
외부 작업을 대기시켜도 시작·재개 API가 먼저 `202`를 반환하는지, 백그라운드 실패가 이벤트로 전달되는지도 검증합니다.
실제 BE·Business MCP·RAG MCP·LLM을 함께 실행한 시연 검증은 별도입니다.

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
