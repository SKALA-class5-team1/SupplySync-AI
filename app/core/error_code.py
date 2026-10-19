"""
에러 코드 정의
"""
from enum import Enum
from http import HTTPStatus


class ErrorCode(Enum):
    """에러 코드 Enum"""
    
    # 성공 코드
    API_SUCCESS = ("000-000", "API_SUCCESS", "요청이 성공적으로 처리되었습니다.", HTTPStatus.OK)
    
    # 공통 에러 (1000번대)
    INTERNAL_SERVER_ERROR = ("E1000", "내부 서버 오류가 발생했습니다.", "서버 내부에서 예상치 못한 오류가 발생했습니다.", HTTPStatus.INTERNAL_SERVER_ERROR)
    INVALID_PARAMETER = ("E1001", "잘못된 파라미터입니다.", "요청 파라미터가 유효하지 않습니다.", HTTPStatus.BAD_REQUEST)
    MISSING_PARAMETER = ("E1002", "필수 파라미터가 누락되었습니다.", "필수 요청 파라미터가 누락되었습니다.", HTTPStatus.BAD_REQUEST)
    INVALID_JSON_FORMAT = ("E1003", "잘못된 JSON 형식입니다.", "요청 본문의 JSON 형식이 올바르지 않습니다.", HTTPStatus.BAD_REQUEST)
    METHOD_NOT_ALLOWED = ("E1004", "지원하지 않는 HTTP 메소드입니다.", "해당 엔드포인트에서 지원하지 않는 HTTP 메소드입니다.", HTTPStatus.METHOD_NOT_ALLOWED)
    RESOURCE_NOT_FOUND = ("E1005", "리소스를 찾을 수 없습니다.", "요청한 리소스를 찾을 수 없습니다.", HTTPStatus.NOT_FOUND)
    TOKEN_NOT_FOUND = ("E1006", "인증 토큰이 없습니다.", "요청 헤더에 인증 토큰이 없습니다.", HTTPStatus.UNAUTHORIZED)
    
    # 인증/인가 에러 (2000번대)
    UNAUTHORIZED = ("E2000", "인증이 필요합니다.", "인증이 필요한 요청입니다.", HTTPStatus.UNAUTHORIZED)
    FORBIDDEN = ("E2001", "접근 권한이 없습니다.", "해당 리소스에 대한 접근 권한이 없습니다.", HTTPStatus.FORBIDDEN)
    TOKEN_EXPIRED = ("E2002", "토큰이 만료되었습니다.", "인증 토큰이 만료되었습니다.", HTTPStatus.UNAUTHORIZED)
    TOKEN_INVALID = ("E2003", "유효하지 않은 토큰입니다.", "인증 토큰이 유효하지 않습니다.", HTTPStatus.UNAUTHORIZED)
    
    # 비즈니스 로직 에러 (3000번대)
    # 필요에 따라 추가
    
    def __init__(self, code: str, message: str, description: str, http_status: HTTPStatus):
        self.code = code
        self.message = message
        self.description = description
        self.http_status = http_status
    
    def get_code(self) -> str:
        """에러 코드 반환"""
        return self.code
    
    def get_message(self) -> str:
        """에러 메시지 반환"""
        return self.message
    
    def get_description(self) -> str:
        """에러 설명 반환"""
        return self.description
    
    def get_http_status(self) -> HTTPStatus:
        """HTTP 상태 코드 반환"""
        return self.http_status

