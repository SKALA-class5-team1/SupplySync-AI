"""SG3 Harness 공통: 한도 상수, 개인정보 마스킹, 결과 자르기, 재시도 판단, 추적 config, 프롬프트 로더

main에 공통 모듈(app/core/guardrails.py, observability.py)과 타임아웃 설정이 아직 없어 SG3 안에 둔다.
공통 모듈이 main에 들어오면 이 파일의 같은 이름 함수·상수를 그쪽 import로 바꾼다.
"""
import asyncio
import os
import re
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

# --- Agent Loop 한도 (표 6-3) ---
MAX_TURNS = 8  # 모델 호출 횟수
TASK_TIMEOUT_SEC = 120.0  # SG3 작업 전체 (사람 승인 대기 시간 제외)
MAX_SAME_TOOL_CALLS = 3  # 같은 도구 호출 횟수

# --- 호출 1회 시간 제한 ---
MCP_CALL_TIMEOUT_SEC = 30.0
MODEL_CALL_TIMEOUT_SEC = 60.0

# --- Recovery ---
RETRY_STATUS = {429, 503}
MAX_RETRY = 2  # 429·503·시간 초과 재시도 횟수
RETRY_BACKOFF_SEC = 0.5  # n번째 재시도 전 대기 = RETRY_BACKOFF_SEC * n
PRIMARY_MODEL = "gpt-4.1-mini"
FALLBACK_MODEL = "gpt-4o-mini"

# --- Context ---
TOKEN_BUDGET = 6000  # 모델 입력 토큰 예산 (근사치)
KEEP_RECENT_MESSAGES = 6  # 요약할 때 그대로 남기는 최근 메시지 수
RAG_TOP_K = 3
RAG_CHUNK_MAX_CHARS = 400  # 근거 문서 조각 하나에 넣는 최대 글자 수
RESULT_MAX_CHARS = 2000  # 도구 결과 문자열 하나의 최대 길이

# --- Guardrails ---
MAX_APPROVAL_INPUTS = 3  # 저장 승인 입력이 잘못 왔을 때 다시 받아 주는 횟수

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "quote_drafting.md"

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 010-1234-5678, 02-123-4567, 01012345678 같은 한국 전화번호
_PHONE = re.compile(r"(?<!\d)0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")
_PROMPT_HEADER = re.compile(r"\A\s*<!--(.*?)-->\s*", re.S)


def mask_pii(text: str) -> str:
    """이메일과 전화번호를 가린다. 문자열이 아니면 그대로 돌려준다."""
    if not isinstance(text, str):
        return text
    return _PHONE.sub("[전화번호]", _EMAIL.sub("[이메일]", text))


def mask_any(value: Any) -> Any:
    """문자열·목록·딕셔너리 안의 문자열을 모두 가린다."""
    if isinstance(value, str):
        return mask_pii(value)
    if isinstance(value, list):
        return [mask_any(v) for v in value]
    if isinstance(value, dict):
        return {k: mask_any(v) for k, v in value.items()}
    return value


def truncate_text(text: str, max_chars: int) -> str:
    """너무 긴 문자열은 앞부분만 남기고 잘렸다고 표시한다."""
    if not isinstance(text, str) or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"…(이하 {len(text) - max_chars}자 생략)"


def truncate_any(value: Any, max_chars: int) -> Any:
    """문자열·목록·딕셔너리 안의 긴 문자열을 모두 자른다."""
    if isinstance(value, str):
        return truncate_text(value, max_chars)
    if isinstance(value, list):
        return [truncate_any(v, max_chars) for v in value]
    if isinstance(value, dict):
        return {k: truncate_any(v, max_chars) for k, v in value.items()}
    return value


def describe_error(exc: BaseException) -> str:
    """상태·로그에 남길 오류 설명. 개인정보를 가리고 길이를 자른다."""
    return truncate_text(mask_pii(f"{type(exc).__name__}: {exc}"), RESULT_MAX_CHARS)


def is_retryable(exc: BaseException) -> bool:
    """HTTP 429·503 이거나 시간 초과면 True. anyio가 묶어 올린 예외(ExceptionGroup)도 안쪽까지 본다."""
    if isinstance(exc, asyncio.TimeoutError):
        return True
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status in RETRY_STATUS:
        return True
    return any(is_retryable(e) for e in getattr(exc, "exceptions", ()))


def run_config(thread_id: str, run_name: str = "sg3_quote_drafting") -> dict[str, Any]:
    """그래프 호출(ainvoke/astream)에 넘길 config. thread_id를 LangSmith trace_id로 남기고, 같은 thread_id로 재개한다.

    LangSmith는 .env의 LANGSMITH_TRACING=true, LANGSMITH_API_KEY로 켠다 (LangChain이 환경변수를 직접 읽음).
    """
    return {
        "configurable": {"thread_id": thread_id},
        "run_name": run_name,
        "tags": ["supplysync", "sg3"],
        "metadata": {"trace_id": thread_id},
    }


def setup_tracing() -> bool:
    """.env의 LANGSMITH_* 값을 LangChain이 읽는 환경변수로 옮긴다. LANGSMITH_TRACING=true이고 키가 있으면 True.

    설정(Settings)은 LANGSMITH_*를 읽지 않아 .env 값이 환경변수로 가지 않으므로 여기서 옮긴다.
    이미 환경변수에 있는 값은 덮어쓰지 않는다. 키는 .env에만 둔다.
    """
    env_file = Path(__file__).resolve().parents[2] / ".env"
    values = dotenv_values(env_file) if env_file.exists() else {}
    for key in ("LANGSMITH_TRACING", "LANGSMITH_API_KEY", "LANGSMITH_PROJECT", "LANGSMITH_ENDPOINT"):
        if values.get(key) and key not in os.environ:
            os.environ[key] = values[key]
    return os.environ.get("LANGSMITH_TRACING", "").lower() == "true" and bool(os.environ.get("LANGSMITH_API_KEY"))


def load_prompt() -> tuple[str, str]:
    """prompts/quote_drafting.md를 읽어 (버전, 본문)을 돌려준다. 맨 위 주석(버전·수정 이력)은 본문에서 뺀다."""
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    header = _PROMPT_HEADER.match(raw)
    version = "unknown"
    if header:
        found = re.search(r"version:\s*(\S+)", header.group(1))
        version = found.group(1) if found else version
        raw = raw[header.end():]
    return version, raw.strip()
