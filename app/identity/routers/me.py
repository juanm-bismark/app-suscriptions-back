from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.identity.auth_utils import hash_password
from app.identity.dependencies import get_current_profile
from app.identity.email_validation import ensure_email_is_available, normalize_email
from app.identity.models.profile import Profile
from app.identity.models.user import User
from app.identity.schemas.profile import ProfileOut, ProfileUpdate

router = APIRouter(prefix="/me", tags=["me"])

CurrentProfile = Annotated[Profile, Depends(get_current_profile)]
DbSession = Annotated[AsyncSession, Depends(get_db)]


async def _attach_profile_email(profile: Profile, db: AsyncSession) -> Profile:
    result = await db.execute(select(User.email).where(User.id == profile.id))
    profile.email = result.scalar_one_or_none()
    return profile


@router.get("", response_model=ProfileOut)
async def get_me(
    current: CurrentProfile,
    db: DbSession,
) -> Profile:
    return await _attach_profile_email(current, db)


@router.patch("", response_model=ProfileOut)
@router.put("", response_model=ProfileOut)
async def update_me(
    body: ProfileUpdate,
    current: CurrentProfile,
    db: DbSession,
) -> Profile:
    password = body.password.strip() if body.password is not None else None
    email = normalize_email(body.email) if body.email is not None else None
    if body.email is not None and not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="email cannot be empty",
        )
    user: User | None = None
    if email is not None or password:
        result = await db.execute(select(User).where(User.id == current.id))
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        if email is not None and email != normalize_email(user.email):
            await ensure_email_is_available(db, email, exclude_user_id=current.id)

    if body.full_name is not None:
        current.full_name = body.full_name

    if user is not None:
        if email is not None:
            user.email = email
        if password:
            user.hashed_password = hash_password(password)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already exists",
        ) from None

    await db.refresh(current)
    return await _attach_profile_email(current, db)
