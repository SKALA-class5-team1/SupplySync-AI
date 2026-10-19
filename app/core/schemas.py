"""
응답 스키마 정의
"""
from typing import Generic, TypeVar, Optional, List, Literal
from pydantic import BaseModel, Field
from app.core.error_code import ErrorCode

T = TypeVar('T')


class ServiceInfoResponse(BaseModel):
    """서비스 기본 정보 조회 응답."""

    message: str = Field(
        description="현재 실행 중인 서비스 이름",
        examples=["SupplySync AI"],
    )
    environment: str = Field(
        description="실행 환경 (로컬 실행 전용으로 항상 local)",
        examples=["local"],
    )
    version: str = Field(
        description="현재 실행 중인 API 버전",
        examples=["1.0.0"],
    )


class HealthCheckResponse(BaseModel):
    """애플리케이션 생존 상태 확인 응답. 외부 의존성 상태는 포함하지 않습니다."""

    status: Literal["healthy"] = Field(
        description="애플리케이션이 HTTP 요청에 응답할 수 있으면 healthy",
        examples=["healthy"],
    )
    environment: str = Field(
        description="실행 환경 (로컬 실행 전용으로 항상 local)",
        examples=["local"],
    )


class Metadata(BaseModel):
    """응답 메타데이터"""
    status: bool = Field(description="요청 성공 여부")
    code: str = Field(description="응답 코드")
    message: str = Field(description="응답 메시지")
    exception: Optional[str] = Field(default=None, description="예외 메시지 (실패 시)")


class BaseResponse(BaseModel, Generic[T]):
    """
    표준 API 응답 스키마
    
    모든 API 응답은 이 형식을 따릅니다.
    - 성공: status=true, code="000-000", result에 데이터 포함
    - 실패: status=false, code=에러코드, exception에 상세 메시지
    """
    metadata: Metadata = Field(description="응답 메타데이터")
    result: Optional[T] = Field(default=None, description="응답 데이터")
    
    @classmethod
    def success(cls, data: Optional[T] = None) -> "BaseResponse[T]":
        """성공 응답 생성"""
        return cls(
            metadata=Metadata(
                status=True,
                code=ErrorCode.API_SUCCESS.code,
                message=ErrorCode.API_SUCCESS.message,
                exception=None
            ),
            result=data
        )
    
    @classmethod
    def success_empty(cls) -> "BaseResponse[None]":
        """데이터 없는 성공 응답 생성"""
        return cls.success(data=None)
    
    @classmethod
    def failure(
        cls,
        error_code: ErrorCode,
        detail_message: Optional[str] = None
    ) -> "BaseResponse[None]":
        """실패 응답 생성"""
        return cls(
            metadata=Metadata(
                status=False,
                code=error_code.code,
                message=error_code.message,
                exception=detail_message or error_code.description
            ),
            result=None
        )


class PageInfo(BaseModel):
    """페이지네이션 정보"""
    page: int = Field(description="현재 페이지 번호 (1부터 시작)")
    size: int = Field(description="페이지 크기")
    total: int = Field(description="전체 항목 수")
    total_pages: int = Field(description="전체 페이지 수")


class PageResponse(BaseModel, Generic[T]):
    """
    페이지네이션 응답 스키마
    
    리스트 조회 시 페이지네이션 정보와 함께 반환합니다.
    """
    metadata: Metadata = Field(description="응답 메타데이터")
    result: Optional[List[T]] = Field(default=None, description="응답 데이터 리스트")
    page_info: Optional[PageInfo] = Field(default=None, description="페이지네이션 정보")
    
    @classmethod
    def success(
        cls,
        data: List[T],
        page: int,
        size: int,
        total: int
    ) -> "PageResponse[T]":
        """성공 응답 생성"""
        total_pages = (total + size - 1) // size if size > 0 else 0
        
        return cls(
            metadata=Metadata(
                status=True,
                code=ErrorCode.API_SUCCESS.code,
                message=ErrorCode.API_SUCCESS.message,
                exception=None
            ),
            result=data,
            page_info=PageInfo(
                page=page,
                size=size,
                total=total,
                total_pages=total_pages
            )
        )

