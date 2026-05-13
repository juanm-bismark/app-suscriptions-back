import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi_pagination import Page, Params
from fastapi_pagination.ext.sqlalchemy import apaginate
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.database import get_db
from app.identity.auth_utils import hash_password
from app.identity.dependencies import get_current_profile, require_roles
from app.identity.email_validation import ensure_email_is_available, normalize_email
from app.identity.models.profile import AppRole, Profile
from app.identity.models.user import User
from app.identity.schemas.profile import ProfileOut
from app.identity.schemas.user import UserCreate, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])

CurrentProfile = Annotated[Profile, Depends(get_current_profile)]
AdminOrManagerProfile = Annotated[
    Profile,
    Depends(require_roles(AppRole.admin, AppRole.manager)),
]
AdminProfile = Annotated[Profile, Depends(require_roles(AppRole.admin))]
DbSession = Annotated[AsyncSession, Depends(get_db)]
PaginationParams = Annotated[Params, Depends()]
SearchQuery = Annotated[str | None, Query()]


async def _attach_profile_email(profile: Profile, db: AsyncSession) -> Profile:
    result = await db.execute(select(User.email).where(User.id == profile.id))
    email = result.scalar_one_or_none()
    profile.email = email
    return profile


async def _attach_profile_emails(
    profiles: list[Profile],
    db: AsyncSession,
) -> list[Profile]:
    if not profiles:
        return profiles
    result = await db.execute(
        select(User.id, User.email).where(
            User.id.in_([profile.id for profile in profiles])
        )
    )
    emails = {row.id: row.email for row in result.all()}
    for profile in profiles:
        profile.email = emails.get(profile.id)
    return profiles


def _list_users_query(current: Profile, q: str | None = None) -> Select[tuple[Profile]]:
    query = select(Profile).where(Profile.company_id == current.company_id)
    if current.role == AppRole.manager:
        query = query.where(Profile.role.in_([AppRole.manager, AppRole.member]))
    if q and (term := q.strip()):
        pattern = f"%{term}%"
        query = query.join(User, User.id == Profile.id).where(
            or_(
                Profile.full_name.ilike(pattern),
                User.email.ilike(pattern),
            )
        )
    return query.order_by(Profile.created_at.desc())


@router.get("", response_model=Page[ProfileOut])
async def list_users(
    current: AdminOrManagerProfile,
    params: PaginationParams,
    db: DbSession,
    q: SearchQuery = None,
) -> Page[ProfileOut]:
    page = await apaginate(db, _list_users_query(current, q), params)
    await _attach_profile_emails(list(page.items), db)
    return page


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProfileOut)
async def create_user(
    body: UserCreate,
    current: CurrentProfile,
    db: DbSession,
) -> Profile:
    if current.role in (AppRole.public, AppRole.member):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )
    if current.role == AppRole.manager and body.role != AppRole.member:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Managers can only create members"
        )

    email = normalize_email(body.email)
    if not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="email cannot be empty",
        )
    await ensure_email_is_available(db, email)

    new_id = uuid.uuid4()
    db.add(User(id=new_id, email=email, hashed_password=hash_password(body.password)))
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already exists",
        ) from None

    await db.refresh(profile)
    profile.email = email
    return profile


@router.get("/{user_id}", response_model=ProfileOut)
async def get_user(
    user_id: uuid.UUID,
    current: CurrentProfile,
    db: DbSession,
) -> Profile:
    if current.role == AppRole.member and current.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    result = await db.execute(
        select(Profile).where(Profile.id == user_id, Profile.company_id == current.company_id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return await _attach_profile_email(profile, db)


@router.patch("/{user_id}", response_model=ProfileOut)
@router.put("/{user_id}", response_model=ProfileOut)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    current: CurrentProfile,
    db: DbSession,
) -> Profile:
    result = await db.execute(
        select(Profile).where(Profile.id == user_id, Profile.company_id == current.company_id)
    )
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if current.role == AppRole.member:
        if current.id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permissions",
            )
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

    password = body.password.strip() if body.password is not None else None
    email = normalize_email(body.email) if body.email is not None else None
    if body.email is not None and not email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="email cannot be empty",
        )
    user: User | None = None
    if email is not None or password:
        result = await db.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        if email is not None and email != normalize_email(user.email):
            await ensure_email_is_available(db, email, exclude_user_id=user_id)

    if body.full_name is not None:
        target.full_name = body.full_name
    if body.role is not None:
        target.role = body.role

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

    await db.refresh(target)
    return await _attach_profile_email(target, db)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    current: AdminProfile,
    db: DbSession,
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
