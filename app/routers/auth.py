import uuid as _uuid
from typing import cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_db
from app.models.company import Company
from app.models.company_settings import CompanySettings

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupRequest(BaseModel):
    email: str
    password: str
    full_name: str | None = None
    company_name: str


@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignupRequest,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    """Register the first admin for a new company."""
    company_id = _uuid.uuid4()
    db.add(Company(id=company_id, name=body.company_name))
    db.add(CompanySettings(company_id=company_id, settings={}))
    await db.commit()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{settings.supabase_url}/auth/v1/admin/users",
            headers={
                "apikey": settings.supabase_service_role_key,
                "Authorization": f"Bearer {settings.supabase_service_role_key}",
            },
            json={
                "email": body.email,
                "password": body.password,
                "email_confirm": True,
                "user_metadata": {
                    "full_name": body.full_name or "",
                    "company_id": str(company_id),
                    "role": "admin",
                },
            },
        )

    if resp.status_code not in (200, 201):
        # Clean up orphaned company
        result = await db.execute(select(Company).where(Company.id == company_id))
        company = result.scalar_one_or_none()
        if company:
            await db.delete(company)
            await db.commit()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=resp.json().get("msg", "Failed to create user"),
        )

    auth_user = cast(dict[str, str], resp.json())
    return {
        "user_id": auth_user["id"],
        "email": auth_user["email"],
        "company_id": str(company_id),
        "role": "admin",
    }
