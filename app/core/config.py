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

    # 선택 기능: database.py, security.py가 사용하는 설정입니다.
    # 이 값들을 정의해도 앱 시작 시 DB 연결이나 인증을 실행하지 않습니다.
    DB_URL: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/supplysync"
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 10
    SECRET_KEY: str = Field(default="local-secret-key-for-development-only", repr=False)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60


settings = Settings()
