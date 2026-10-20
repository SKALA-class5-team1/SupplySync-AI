"""SupplySync AI 로컬 실행 설정. 프로젝트 루트의 .env 하나를 사용합니다."""

from pathlib import Path
from typing import ClassVar

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경 변수 > .env > 기본값 순서로 설정을 읽습니다."""

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[2] / ".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # 로컬 전용이며 ENV/ENVIRONMENT로 환경을 전환하지 않습니다.
    ENVIRONMENT: ClassVar[str] = "local"

    APP_NAME: str = "SupplySync AI"
    APP_VERSION: str = "1.0.0"
    API_V1_PREFIX: str = "/api/v1/supplysync"
    DEBUG: bool = True

    HOST: str = "127.0.0.1"
    PORT: int = 8000
    RELOAD: bool = True
    LOG_LEVEL: str = "DEBUG"

    # .env에서는 JSON 배열로 작성합니다. 예: ["http://localhost:3000"]
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:8080",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8080",
    ]
    OPENAI_API_KEY: str = Field(default="", repr=False)

    # MCP 접속 주소만 설정합니다. 실제 도구 호출은 추후 구현합니다.
    BUSINESS_MCP_URL: str = "http://127.0.0.1:8101/mcp"
    RAG_MCP_URL: str = "http://127.0.0.1:8102/mcp"
    RAG_API_DOCS_URL: str = "http://127.0.0.1:8102/docs"

    # RabbitMQ: 메시지 발행/소비용 AMQP 연결 설정입니다.
    # RABBITMQ_MGMT_PORT는 관리 화면용이므로 앱 연결에는 사용하지 않습니다.
    RABBITMQ_HOST: str = "localhost"
    RABBITMQ_AMQP_PORT: int = Field(default=5672, ge=1, le=65535)
    RABBITMQ_USER: str = "supplysync"
    RABBITMQ_PASSWORD: str = Field(default="changeme", repr=False)

    # Spring Boot 내부 API. FastAPI의 PORT(8000)와 구분합니다.
    SERVER_PORT: int = Field(default=8080, ge=1, le=65535)
    INTERNAL_API_KEY: str = Field(default="changeme-internal-key", repr=False)

    @property
    def BACKEND_BASE_URL(self) -> str:
        """로컬 Spring Boot 백엔드 주소."""
        return f"http://localhost:{self.SERVER_PORT}"

    # 선택: AI 서버 자체의 JWT 인증을 구현할 때 사용합니다.
    SECRET_KEY: str = Field(default="local-secret-key-for-development-only", repr=False)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60


settings = Settings()
