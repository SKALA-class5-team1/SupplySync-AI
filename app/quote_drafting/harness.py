"""SG3 Harness 공통: 한도 상수, 오류 설명, 재시도 판단, 프롬프트 로더

마스킹·자르기는 app/core/guardrails.py, 추적 config는 app/core/observability.py를 쓴다.
MCP 호출 1회 시간과 결과 길이는 설정(MCP_CALL_TIMEOUT_SEC, MCP_RESULT_MAX_CHARS)을 따른다.
"""
import asyncio
import re
from pathlib import Path

from app.core.config import settings
from app.core.guardrails import mask_pii, truncate_text

# --- Agent Loop 한도 (표 6-3) ---
MAX_TURNS = 8  # 모델 호출 횟수
TASK_TIMEOUT_SEC = 120.0  # SG3 작업 전체 (사람 승인 대기 시간 제외)
MAX_SAME_TOOL_CALLS = 3  # 같은 도구 호출 횟수

# --- 호출 1회 시간 제한 ---
MCP_CALL_TIMEOUT_SEC = settings.MCP_CALL_TIMEOUT_SEC
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
RESULT_MAX_CHARS = settings.MCP_RESULT_MAX_CHARS  # 도구 결과 문자열 하나의 최대 길이

# --- 견적 차수 ---
MAX_REVISION = 4  # 견적 revision 최대 4 = SG4 수정 요청(REVISE) 최대 3회 (상위 그래프 MAX_REVISIONS와 같게)

# --- Guardrails ---
MAX_APPROVAL_INPUTS = 3  # 저장 승인 입력이 잘못 왔을 때 다시 받아 주는 횟수

# --- Observability ---
RUN_NAME = "sg3_quote_drafting"  # observability.run_config(thread_id, RUN_NAME)

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "quote_drafting.md"

_PROMPT_HEADER = re.compile(r"\A\s*<!--(.*?)-->\s*", re.S)


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
