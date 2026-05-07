import uuid
from typing import Awaitable, Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_db
from app.identity.models.profile import AppRole, Profile

_bearer = HTTPBearer()
_bearer_optional = HTTPBearer(auto_error=False)


async def get_current_profile(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> Profile:
    try:
        if not settings.jwt_secret:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Missing JWT secret")
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=["HS256"],
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id: str | None = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    result = await db.execute(select(Profile).where(Profile.id == uuid.UUID(user_id)))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return profile


async def get_current_profile_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_optional),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> Profile | None:
    if not credentials:
        return None
    try:
        if not settings.jwt_secret:
            return None
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=["HS256"],
        )
    except (jwt.ExpiredSignatureError, jwt.PyJWTError):
        return None
    user_id: str | None = payload.get("sub")
    if not user_id:
        return None
    result = await db.execute(select(Profile).where(Profile.id == uuid.UUID(user_id)))
    return result.scalar_one_or_none()


def require_roles(*roles: AppRole) -> "Callable[[Profile], Awaitable[Profile]]":
    async def _checker(profile: Profile = Depends(get_current_profile)) -> Profile:
        if profile.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return profile

    return _checker


async def get_current_company_id(
    profile: Profile = Depends(get_current_profile),
) -> uuid.UUID:
    """Return the caller's company_id, narrowed to UUID.

    Profiles without a company scope (e.g. role=public) cannot use any
    tenant-scoped endpoint — raise 403.
    """
    if profile.company_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Profile is not scoped to a company",
        )
    return profile.company_id
