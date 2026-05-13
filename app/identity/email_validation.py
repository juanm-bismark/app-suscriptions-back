import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.identity.models.user import User


def normalize_email(email: str) -> str:
    return email.strip().casefold()


async def ensure_email_is_available(
    db: AsyncSession,
    email: str,
    *,
    exclude_user_id: uuid.UUID | None = None,
) -> None:
    normalized_email = normalize_email(email)
    query = select(User.id).where(func.lower(User.email) == normalized_email)
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)

    result = await db.execute(query)
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already exists",
        )
