from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_profile, require_roles
from app.models.company import Company
from app.models.company_settings import CompanySettings
from app.models.profile import AppRole, Profile
from app.schemas.company import CompanyOut, CompanySettingsOut, CompanySettingsUpdate, CompanyUpdate

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("/me", response_model=CompanyOut)
async def get_my_company(
    current: Profile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> Company:
    result = await db.execute(select(Company).where(Company.id == current.company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    return company


@router.put("/me", response_model=CompanyOut)
async def update_my_company(
    body: CompanyUpdate,
    current: Profile = Depends(require_roles(AppRole.admin)),
    db: AsyncSession = Depends(get_db),
) -> Company:
    result = await db.execute(select(Company).where(Company.id == current.company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Company not found")
    company.name = body.name
    await db.commit()
    await db.refresh(company)
    return company


@router.get("/me/settings", response_model=CompanySettingsOut)
async def get_my_settings(
    current: Profile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> CompanySettings:
    result = await db.execute(
        select(CompanySettings).where(CompanySettings.company_id == current.company_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Settings not found")
    return row


@router.put("/me/settings", response_model=CompanySettingsOut)
async def update_my_settings(
    body: CompanySettingsUpdate,
    current: Profile = Depends(require_roles(AppRole.admin)),
    db: AsyncSession = Depends(get_db),
) -> CompanySettings:
    result = await db.execute(
        select(CompanySettings).where(CompanySettings.company_id == current.company_id)
    )
    row = result.scalar_one_or_none()
    if row:
        row.settings = body.settings
    else:
        row = CompanySettings(company_id=current.company_id, settings=body.settings)
        db.add(row)
    await db.commit()
    await db.refresh(row)
    return row
