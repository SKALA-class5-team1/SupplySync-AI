"""도구 결과·로그에 개인정보가 그대로 남지 않도록 가리는 함수 (Harness: Guardrails)."""
import re
from typing import Any

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
# 010-1234-5678, 02-123-4567, 01012345678 같은 한국 전화번호
_PHONE = re.compile(r"(?<!\d)0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4}(?!\d)")


def mask_pii(text: str) -> str:
    """이메일과 전화번호를 가린다. 문자열이 아니면 그대로 돌려준다."""
    if not isinstance(text, str):
        return text
    text = _EMAIL.sub("[이메일]", text)
    return _PHONE.sub("[전화번호]", text)


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
    """너무 긴 문자열은 앞부분만 남기고 잘렸다고 표시한다 (Harness: Tools)."""
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
