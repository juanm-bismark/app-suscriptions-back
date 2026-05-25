import uuid
from typing import Annotated, cast as typing_cast

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi_pagination import Page, Params
from fastapi_pagination.ext.sqlalchemy import apaginate
from sqlalchemy import String, cast, or_, select
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
from app.tenancy.models.company import Company

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


def _profile_out_from_email(profile: Profile, email: str | None) -> ProfileOut:
    return ProfileOut.model_construct(
        id=profile.id,
        company_id=profile.company_id,
        email=email,
        role=profile.role,
        full_name=profile.full_name,
        created_at=profile.created_at,
    )


async def _profile_out(profile: Profile, db: AsyncSession) -> ProfileOut:
    result = await db.execute(select(User.email).where(User.id == profile.id))
    return _profile_out_from_email(profile, result.scalar_one_or_none())


async def _attach_profile_emails(
    profiles: list[Profile],
    db: AsyncSession,
) -> list[ProfileOut]:
    if not profiles:
        return []
    result = await db.execute(
        select(User.id, User.email).where(
            User.id.in_([profile.id for profile in profiles])
        )
    )
    emails = {row.id: row.email for row in result.all()}
    out: list[ProfileOut] = []
    for profile in profiles:
        email = emails.get(profile.id)
        object.__setattr__(profile, "email", email)
        out.append(_profile_out_from_email(profile, email))
    return out


async def _get_company_or_404(
    db: AsyncSession,
    company_id: str,
) -> Company:
    company_id_text = company_id.strip()
    if not company_id_text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="company_id cannot be empty",
        )

    result = await db.execute(
        select(Company).where(cast(Company.id, String) == company_id_text)
    )
    company = result.scalar_one_or_none()
    if company is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Company '{company_id_text}' not found",
        )
    return company


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
    items = await _attach_profile_emails(list(page.items), db)
    return Page.create(items, params, total=typing_cast(int, page.total))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=ProfileOut)
async def create_user(
    body: UserCreate,
    current: CurrentProfile,
    db: DbSession,
) -> ProfileOut:
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

    if current.role == AppRole.admin:
        if body.company_id is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="company_id is required when creating a user as admin",
            )
        company_id = body.company_id
    else:
        if current.company_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Profile is not scoped to a company",
            )
        company_id = current.company_id

    new_id = uuid.uuid4()
    db.add(User(id=new_id, email=email, hashed_password=hash_password(body.password)))
    profile = Profile(
        id=new_id,
        company_id=company_id,
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
    return _profile_out_from_email(profile, email)


@router.get("/{user_id}", response_model=ProfileOut)
async def get_user(
    user_id: uuid.UUID,
    current: CurrentProfile,
    db: DbSession,
) -> ProfileOut:
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
    return await _profile_out(profile, db)


@router.patch("/{user_id}", response_model=ProfileOut)
@router.put("/{user_id}", response_model=ProfileOut)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    current: AdminOrManagerProfile,
    db: DbSession,
) -> ProfileOut:
    if current.role not in (AppRole.admin, AppRole.manager):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )

    query = select(Profile).where(Profile.id == user_id)
    if current.role != AppRole.admin:
        query = query.where(Profile.company_id == current.company_id)
    result = await db.execute(query)
    target = result.scalar_one_or_none()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if current.role == AppRole.manager:
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
        user_result = await db.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found",
            )
        if email is not None and email != normalize_email(user.email):
            await ensure_email_is_available(db, email, exclude_user_id=user_id)

    company: Company | None = None
    if body.company_id is not None and current.role == AppRole.admin:
        company = await _get_company_or_404(db, body.company_id)

    if body.full_name is not None:
        target.full_name = body.full_name
    if body.role is not None:
        target.role = body.role
    if company is not None:
        target.company_id = company.id

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
    return await _profile_out(target, db)


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
