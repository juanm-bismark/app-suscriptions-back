import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.tenancy.models.company import Company


def normalize_company_name(name: str) -> str:
    return name.strip()


async def ensure_company_name_is_available(
    db: AsyncSession,
    name: str,
    *,
    exclude_company_id: uuid.UUID | None = None,
) -> None:
    normalized_name = normalize_company_name(name)
    query = select(Company.id).where(func.lower(Company.name) == normalized_name.casefold())
    if exclude_company_id is not None:
        query = query.where(Company.id != exclude_company_id)

    result = await db.execute(query)
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Company name already exists",
        )
