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
