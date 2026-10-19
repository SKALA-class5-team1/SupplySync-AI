"""
FastAPI 의존성 함수들
인증 및 권한 검사 등
"""
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.core.security import verify_token

# HTTP Bearer 토큰 스키마
security = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> dict:
    """
    현재 요청의 JWT 토큰을 검증하고 사용자 정보를 반환합니다.
    
    사용 예시:
        @router.get("/protected")
        async def protected_route(user: dict = Depends(get_current_user)):
            return {"user_id": user["sub"]}
    
    Args:
        credentials: HTTP Bearer 토큰
        
    Returns:
        토큰에서 디코딩된 사용자 정보
        
    Raises:
        HTTPException: 토큰이 유효하지 않을 때
    """
    token = credentials.credentials
    payload = verify_token(token)
    
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효하지 않은 인증 토큰입니다",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return payload


async def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False))
) -> Optional[dict]:
    """
    선택적 인증 - 토큰이 있으면 검증하고, 없으면 None을 반환합니다.
    
    Args:
        credentials: HTTP Bearer 토큰 (선택적)
        
    Returns:
        토큰이 유효하면 사용자 정보, 없거나 유효하지 않으면 None
    """
    if credentials is None:
        return None
    
    token = credentials.credentials
    return verify_token(token)

