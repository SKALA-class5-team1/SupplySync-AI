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
로그는 콘솔에만 출력하며 DB와 API 키 없이 서버를 시작할 수 있습니다.

## core 파일 정리 기준

현재 `main.py`와 도메인 코드의 import를 기준으로 구분했습니다. 미사용 파일은 삭제하지 않았습니다.
기존 `config/`는 `config.py`와 이름이 겹치지 않도록 `config_legacy/`로 옮겨 보관했습니다. 현재 앱에서는 이 폴더를 사용하지 않습니다.

| 분류 | 파일 | 이유 / 필요한 시점 |
| --- | --- | --- |
| 유지 | `__init__.py`, `config.py` | 패키지 구조와 단일 로컬 설정 |
| 유지 | `schemas.py` | 시스템 API 응답 모델과 공통 응답 형식 |
| 유지 | `exceptions.py`, `error_code.py` | 현재 등록된 예외 처리와 오류 코드 |
| 유지 | `middleware.py`, `logging_config.py` | 현재 등록된 요청 추적·로깅·보안 헤더. 파일 삭제 전에는 `main.py` 연결도 제거해야 함 |
| 현재 불필요 | `health.py` | 라우터가 등록되어 있지 않음. 현재 `/health`는 `main.py`에 구현됨 |
| 현재 불필요 | `validation.py` | 중복값 검사 함수를 호출하는 코드가 없음 |
| 현재 불필요 | `config_legacy/` 전체 | 이전 환경별 설정 보관본. 현재 설정에서 참조하지 않음 |
| 기능 도입 시 필요 | `database.py` | AI 서버가 직접 DB를 조회·저장할 때. 현재 모델과 업무 코드의 사용이 없음 |
| 기능 도입 시 필요 | `dependencies.py`, `security.py` | AI 서버에서 JWT 인증을 처리할 때. `dependencies.py`가 `security.py`를 참조하므로 함께 판단 |
| 기능 도입 시 필요 | `storage.py` | MinIO 파일 저장을 도입할 때. 현재 미사용이며 이전 프로젝트 버킷명이 남아 있음 |
| 기능 도입 시 필요 | `http_client.py` | 외부 HTTP API 호출을 공통화할 때. 현재 미사용이며 도입 시 클라이언트 종료 처리 필요 |

DB를 사용하지 않는다면 `database.py`와 이를 참조하는 미등록 `health.py`를 함께 정리할 수 있습니다.
DB·인증 파일을 정리한 뒤에는 `config.py`의 해당 선택 설정도 제거할 수 있습니다.

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

응답 예시는 로컬 설정 기준입니다. `/health`는 DB·외부 API·AI 모델 연결을 검사하지 않습니다.
