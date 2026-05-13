import uuid as _uuid
from datetime import UTC, datetime
from typing import Annotated, TypedDict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_db
from app.identity.auth_utils import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from app.identity.dependencies import get_current_profile_optional
from app.identity.email_validation import ensure_email_is_available, normalize_email
from app.identity.models.profile import AppRole, Profile
from app.identity.models.refresh_token import RefreshToken
from app.identity.models.user import User
from app.tenancy.company_validation import (
    ensure_company_name_is_available,
    normalize_company_name,
)
from app.tenancy.models.company import Company
from app.tenancy.models.company_settings import CompanySettings

router = APIRouter(prefix="/auth", tags=["auth"])

DbSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
OptionalCurrentProfile = Annotated[Profile | None, Depends(get_current_profile_optional)]


class SignupRequest(BaseModel):
    email: str
    password: str
    full_name: str | None = None
    company_name: str | None = None
    role: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(TypedDict):
    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str


def _build_token_response(user_id: _uuid.UUID, raw_refresh: str, settings: Settings) -> TokenResponse:
    jwt_secret = settings.jwt_secret
    if jwt_secret is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="JWT secret is not configured",
        )

    access_token = create_access_token(str(user_id), jwt_secret, settings.jwt_expire_minutes)
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": settings.jwt_expire_minutes * 60,
        "refresh_token": raw_refresh,
    }


@router.post("/login", status_code=status.HTTP_200_OK)
async def login(
    body: LoginRequest,
    db: DbSession,
    settings: SettingsDep,
) -> TokenResponse:
    email = normalize_email(body.email)
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")

    raw, expires_at = generate_refresh_token()
    db.add(RefreshToken(
        user_id=user.id,
        token=hash_refresh_token(raw),
        expires_at=expires_at,
        created_at=datetime.now(UTC),
    ))
    await db.commit()
    return _build_token_response(user.id, raw, settings)


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignupRequest,
    db: DbSession,
    settings: SettingsDep,
    current: OptionalCurrentProfile,
) -> TokenResponse:
    user_id = _uuid.uuid4()

    email = normalize_email(body.email)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="email cannot be empty",
        )
    await ensure_email_is_available(db, email)

    if current is None:
        company_name = normalize_company_name(body.company_name or "")
        if not company_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="company_name required for signup without authentication",
            )
        await ensure_company_name_is_available(db, company_name)
        company_id = _uuid.uuid4()
        role = AppRole.public
    else:
        if current.company_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot invite users without a company scope",
            )
        company_id = current.company_id
        requested_role_str = body.role or "member"
        try:
            requested_role = AppRole(requested_role_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid role: {requested_role_str}",
            ) from None

        if current.role == AppRole.admin:
            role = requested_role
        elif current.role == AppRole.manager:
            if requested_role not in (AppRole.member, AppRole.public):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Managers can only create members or public users",
                )
            role = requested_role
        else:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only admins and managers can create users",
            )

    db.add(User(id=user_id, email=email, hashed_password=hash_password(body.password)))

    if current is None:
        db.add(Company(id=company_id, name=company_name))
        db.add(CompanySettings(company_id=company_id, settings={}))

    db.add(Profile(id=user_id, company_id=company_id, role=role, full_name=body.full_name or ""))

    raw, expires_at = generate_refresh_token()
    db.add(RefreshToken(
        user_id=user_id,
        token=hash_refresh_token(raw),
        expires_at=expires_at,
        created_at=datetime.now(UTC),
    ))

    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        orig = str(e.orig) if e.orig else str(e)
        if "users_email_key" in orig or "user_email_key" in orig:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email already exists",
            ) from None
        if "companies_name_key" in orig:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Company name already exists",
            ) from None
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resource already exists",
        ) from None

    return _build_token_response(user_id, raw, settings)


@router.post("/refresh", status_code=status.HTTP_200_OK)
async def refresh(
    body: RefreshRequest,
    db: DbSession,
    settings: SettingsDep,
) -> TokenResponse:
    hashed = hash_refresh_token(body.refresh_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token == hashed))
    record = result.scalar_one_or_none()

    if not record:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")

    if record.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        await db.delete(record)
        await db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired")

    await db.delete(record)
    raw, new_expires_at = generate_refresh_token()
    db.add(RefreshToken(
        user_id=record.user_id,
        token=hash_refresh_token(raw),
        expires_at=new_expires_at,
        created_at=datetime.now(UTC),
    ))
    await db.commit()
    return _build_token_response(record.user_id, raw, settings)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: RefreshRequest,
    db: DbSession,
) -> None:
    hashed = hash_refresh_token(body.refresh_token)
    result = await db.execute(select(RefreshToken).where(RefreshToken.token == hashed))
    record = result.scalar_one_or_none()
    if record:
        await db.delete(record)
        await db.commit()
