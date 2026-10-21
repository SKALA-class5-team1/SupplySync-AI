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
RAG MCP는 서버가 준비되면 같은 공통 클라이언트에 추가합니다. RabbitMQ는 접속 설정만 준비된 상태입니다.

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
