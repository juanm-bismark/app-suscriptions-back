import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.identity.auth_utils import hash_password
from app.identity.dependencies import get_current_profile, require_roles
from app.identity.models.profile import AppRole, Profile
from app.identity.models.user import User
from app.identity.schemas.profile import ProfileOut
from app.identity.schemas.user import UserCreate, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("", response_model=list[ProfileOut])
async def list_users(
    current: Profile = Depends(require_roles(AppRole.admin, AppRole.manager)),
    db: AsyncSession = Depends(get_db),
) -> list[Profile]:
    result = await db.execute(select(Profile).where(Profile.company_id == current.company_id))
    return list(result.scalars().all())


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProfileOut)
async def create_user(
    body: UserCreate,
    current: Profile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> Profile:
    if current.role == AppRole.member:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
    if current.role == AppRole.manager and body.role != AppRole.member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Managers can only create members"
        )

    new_id = uuid.uuid4()
    db.add(User(id=new_id, email=body.email, hashed_password=hash_password(body.password)))
    profile = Profile(
        id=new_id,
        company_id=current.company_id,
        role=body.role,
        full_name=body.full_name or "",
    )
    db.add(profile)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already exists")

    await db.refresh(profile)
    return profile


@router.get("/{user_id}", response_model=ProfileOut)
async def get_user(
    user_id: uuid.UUID,
    current: Profile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> Profile:
    if current.role == AppRole.member and current.id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")

    result = await db.execute(
        select(Profile).where(Profile.id == user_id, Profile.company_id == current.company_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return profile


@router.put("/{user_id}", response_model=ProfileOut)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    current: Profile = Depends(get_current_profile),
    db: AsyncSession = Depends(get_db),
) -> Profile:
    result = await db.execute(
        select(Profile).where(Profile.id == user_id, Profile.company_id == current.company_id)
    )
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if current.role == AppRole.member:
        if current.id != user_id:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        if body.role is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Members cannot change roles"
            )
    elif current.role == AppRole.manager:
        if target.role != AppRole.member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Managers can only edit members"
            )
        if body.role is not None and body.role != AppRole.member:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Managers cannot promote beyond member",
            )

    if body.full_name is not None:
        target.full_name = body.full_name
    if body.role is not None:
        target.role = body.role

    await db.commit()
    await db.refresh(target)
    return target


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    current: Profile = Depends(require_roles(AppRole.admin)),
    db: AsyncSession = Depends(get_db),
) -> None:
    if current.id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete your own account"
        )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    result2 = await db.execute(
        select(Profile).where(Profile.id == user_id, Profile.company_id == current.company_id)
    )
    if not result2.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    await db.delete(user)
    await db.commit()
