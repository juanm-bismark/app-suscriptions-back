from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.identity.dependencies import get_current_profile
from app.identity.models.profile import Profile
from app.identity.schemas.profile import ProfileOut, ProfileUpdate

router = APIRouter(prefix="/me", tags=["me"])


@router.get("", response_model=ProfileOut)
async def get_me(current: Profile = Depends(get_current_profile)) -> Profile:
    return current


@router.put("", response_model=ProfileOut)
async def update_me(
    body: ProfileUpdate,
    current: Profile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> Profile:
    if body.full_name is not None:
        current.full_name = body.full_name
    await db.commit()
    await db.refresh(current)
    return current
