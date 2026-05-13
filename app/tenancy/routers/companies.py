import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi_pagination import Page, Params
from fastapi_pagination.ext.sqlalchemy import apaginate
from sqlalchemy import delete, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.config import Settings, get_settings, require_fernet_key
from app.database import get_db
from app.identity.dependencies import require_roles
from app.identity.models.profile import AppRole, Profile
from app.identity.models.user import User
from app.providers.base import Provider
from app.providers.moabits.adapter import fetch_child_companies
from app.shared.crypto import decrypt_credentials
from app.tenancy.company_validation import (
    ensure_company_name_is_available,
    normalize_company_name,
)
from app.tenancy.models.company import Company
from app.tenancy.models.company_settings import CompanySettings
from app.tenancy.models.credentials import CompanyProviderCredentials
from app.tenancy.models.moabits_source_company import MoabitsSourceCompany
from app.tenancy.models.provider_mapping import CompanyProviderMapping
from app.tenancy.schemas.company import (
    CompanyCreate,
    CompanyOut,
    CompanyProviderMappingOut,
    CompanyProviderMappingUpdate,
    CompanySettingsOut,
    CompanySettingsUpdate,
    CompanyUpdate,
    LocalCompanyProviderMappingOut,
    MoabitsLinkedCompanyOut,
    MoabitsProviderCompanyOut,
    MoabitsProviderMappingDiscoveryOut,
    MoabitsSourceCompanyOut,
)

router = APIRouter(prefix="/companies", tags=["companies"])
MOABITS_DISCOVERY_CACHE_MESSAGE = (
    "Discovery refreshes the cached Moabits source companies used as link "
    "options. Existing saved local-company mappings are not deleted, but "
    "available Moabits company codes/names may change."
)

AdminProfile = Annotated[Profile, Depends(require_roles(AppRole.admin))]
ManagerOrAdminProfile = Annotated[
    Profile,
    Depends(require_roles(AppRole.manager, AppRole.admin)),
]
CompanyProfile = Annotated[
    Profile,
    Depends(require_roles(AppRole.admin, AppRole.manager, AppRole.member)),
]
DbSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
PageParams = Annotated[Params, Depends()]
SearchQuery = Annotated[str | None, Query()]


def _moabits_company_row(item: dict[str, Any]) -> dict[str, Any] | None:
    company_code = str(
        item.get("companyCode") or item.get("company_code") or ""
    ).strip()
    company_name = str(
        item.get("companyName") or item.get("company_name") or ""
    ).strip()
    if not company_code:
        return None
    clie_id = item.get("clie_id", item.get("clieId"))
    return {
        "company_code": company_code,
        "company_name": company_name,
        "clie_id": clie_id if isinstance(clie_id, int) else None,
        "raw_payload": dict(item),
    }


async def _get_company_or_404(company_id: uuid.UUID, db: AsyncSession) -> Company:
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Company not found",
        )
    return company


async def _active_provider_credential(
    company_id: uuid.UUID | None,
    provider: Provider,
    db: AsyncSession,
) -> CompanyProviderCredentials | None:
    result = await db.execute(
        select(CompanyProviderCredentials).where(
            CompanyProviderCredentials.company_id == company_id,
            CompanyProviderCredentials.provider == provider.value,
            CompanyProviderCredentials.active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def _get_mapping(
    company_id: uuid.UUID,
    provider: Provider,
    db: AsyncSession,
) -> CompanyProviderMapping | None:
    result = await db.execute(
        select(CompanyProviderMapping).where(
            CompanyProviderMapping.company_id == company_id,
            CompanyProviderMapping.provider == provider.value,
        )
    )
    return result.scalar_one_or_none()


async def _cached_moabits_source_company(
    source_company_id: uuid.UUID | None,
    provider_company_code: str,
    db: AsyncSession,
) -> MoabitsSourceCompany | None:
    if source_company_id is None:
        return None
    result = await db.execute(
        select(MoabitsSourceCompany).where(
            MoabitsSourceCompany.source_company_id == source_company_id,
            MoabitsSourceCompany.company_code == provider_company_code,
            MoabitsSourceCompany.active.is_(True),
        )
    )
    row = result.scalar_one_or_none()
    return row if isinstance(row, MoabitsSourceCompany) else None


def _source_company_as_discovered(row: MoabitsSourceCompany) -> dict[str, Any]:
    return {
        "company_code": row.company_code,
        "company_name": row.company_name,
        "clie_id": row.clie_id,
    }


async def _cache_moabits_source_companies(
    source_company_id: uuid.UUID,
    rows: list[dict[str, Any]],
    db: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    rows_by_code = {row["company_code"]: row for row in rows}
    existing_result = await db.execute(
        select(MoabitsSourceCompany).where(
            MoabitsSourceCompany.source_company_id == source_company_id,
        )
    )
    existing_by_code = {
        row.company_code: row
        for row in existing_result.scalars().all()
        if isinstance(row, MoabitsSourceCompany)
    }

    for company_code, source_company in existing_by_code.items():
        if company_code not in rows_by_code and source_company.active:
            source_company.active = False
            source_company.updated_at = now

    for row in rows_by_code.values():
        source_company = existing_by_code.get(row["company_code"])
        if source_company is None:
            source_company = MoabitsSourceCompany(
                source_company_id=source_company_id,
                company_code=row["company_code"],
                company_name=row["company_name"],
                clie_id=row["clie_id"],
                raw_payload=row.get("raw_payload") or {},
                last_seen_at=now,
                active=True,
                created_at=now,
                updated_at=now,
            )
            db.add(source_company)
            continue

        source_company.company_name = row["company_name"]
        source_company.clie_id = row["clie_id"]
        source_company.raw_payload = row.get("raw_payload") or {}
        source_company.last_seen_at = now
        source_company.active = True
        source_company.updated_at = now
    await db.commit()


async def _validate_moabits_mapping_code(
    provider_company_code: str,
    source_company_id: uuid.UUID | None,
    db: AsyncSession,
    settings: Settings,
) -> dict[str, Any] | None:
    cached_company = await _cached_moabits_source_company(
        source_company_id,
        provider_company_code,
        db,
    )
    if cached_company is not None:
        return _source_company_as_discovered(cached_company)

    credential = await _active_provider_credential(
        source_company_id, Provider.MOABITS, db
    )
    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "message": "Moabits company code is not available to this X-API-KEY",
                "company_code": provider_company_code,
            },
        )
    credentials = decrypt_credentials(
        credential.credentials_enc,
        require_fernet_key(settings),
    )
    provider_companies = {
        row["company_code"]: row
        for item in await fetch_child_companies(credentials)
        if (row := _moabits_company_row(item)) is not None
    }
    discovered_company = provider_companies.get(provider_company_code)
    if discovered_company is not None:
        return discovered_company
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "message": "Moabits company code is not available to this X-API-KEY",
            "company_code": provider_company_code,
        },
    )


def _list_companies_query(q: str | None = None) -> Select[tuple[Company]]:
    query = select(Company)
    if q and (term := q.strip()):
        query = query.where(Company.name.ilike(f"%{term}%"))
    return query.order_by(Company.created_at.desc(), Company.name)


def _list_moabits_source_companies_query(
    source_company_id: uuid.UUID,
    q: str | None = None,
    *,
    active_only: bool = True,
) -> Select[tuple[MoabitsSourceCompany]]:
    query = select(MoabitsSourceCompany).where(
        MoabitsSourceCompany.source_company_id == source_company_id,
    )
    if active_only:
        query = query.where(MoabitsSourceCompany.active.is_(True))
    if q and (term := q.strip()):
        pattern = f"%{term}%"
        query = query.where(
            or_(
                MoabitsSourceCompany.company_code.ilike(pattern),
                MoabitsSourceCompany.company_name.ilike(pattern),
            )
        )
    return query.order_by(
        MoabitsSourceCompany.company_name,
        MoabitsSourceCompany.company_code,
    )


@router.get("", response_model=Page[CompanyOut])
async def list_companies(
    current: AdminProfile,
    params: PageParams,
    db: DbSession,
    q: SearchQuery = None,
) -> Page[CompanyOut]:
    """List all companies with optional name search. Admin only."""
    return await apaginate(db, _list_companies_query(q), params)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=CompanyOut)
async def create_company(
    body: CompanyCreate,
    current: AdminProfile,
    db: DbSession,
) -> Company:
    """Create a new company. Admin only."""
    name = normalize_company_name(body.name)
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="name cannot be empty",
        )
    await ensure_company_name_is_available(db, name)

    company = Company(id=uuid.uuid4(), name=name)
    db.add(company)
    db.add(CompanySettings(company_id=company.id, settings={}))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Company name already exists",
        ) from None

    await db.refresh(company)
    return company


@router.get("/me", response_model=CompanyOut)
async def get_my_company(
    current: CompanyProfile,
    db: DbSession,
) -> Company:
    """Get the company associated with the authenticated user. Any authenticated role."""
    result = await db.execute(select(Company).where(Company.id == current.company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Company not found"
        )
    return company


@router.put("/me", response_model=CompanyOut)
async def update_my_company(
    body: CompanyUpdate,
    current: AdminProfile,
    db: DbSession,
) -> Company:
    """Rename the company associated with the authenticated user. Admin only."""
    result = await db.execute(select(Company).where(Company.id == current.company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Company not found"
        )
    name = normalize_company_name(body.name)
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="name cannot be empty",
        )
    if name.casefold() != company.name.casefold():
        await ensure_company_name_is_available(db, name, exclude_company_id=company.id)
    company.name = name
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Company name already exists",
        ) from None

    await db.refresh(company)
    return company


@router.get(
    "/me/provider-mappings/{provider}",
    response_model=CompanyProviderMappingOut,
)
async def get_my_provider_mapping(
    provider: Provider,
    current: ManagerOrAdminProfile,
    db: DbSession,
) -> CompanyProviderMapping:
    """Get the active Moabits mapping for the current user's company. Manager or admin."""
    if provider != Provider.MOABITS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only Moabits company mappings are supported",
        )
    if current.company_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Profile is not scoped to a company",
        )
    mapping = await _get_mapping(current.company_id, provider, db)
    if mapping is None or not mapping.active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Provider mapping not found",
        )
    return mapping


@router.get(
    "/provider-mappings/moabits",
    response_model=Page[LocalCompanyProviderMappingOut],
)
async def list_moabits_provider_mappings(
    current: AdminProfile,
    db: DbSession,
    params: PageParams,
    q: SearchQuery = None,
    linked_only: bool = Query(default=False),
) -> Page[LocalCompanyProviderMappingOut]:
    """List all local companies with their Moabits mapping status. No external API call. Admin only."""
    local_result = await db.execute(select(Company).order_by(Company.name))
    local_companies = list(local_result.scalars().all())

    mapping_result = await db.execute(
        select(CompanyProviderMapping).where(
            CompanyProviderMapping.provider == Provider.MOABITS.value,
            CompanyProviderMapping.active.is_(True),
        )
    )
    mappings_by_company_id = {
        m.company_id: m for m in mapping_result.scalars().all()
    }

    search_term = q.strip().casefold() if q and q.strip() else None
    rows: list[LocalCompanyProviderMappingOut] = []
    for company in local_companies:
        mapping = mappings_by_company_id.get(company.id)
        if linked_only and mapping is None:
            continue
        if search_term:
            haystack = company.name.casefold()
            if mapping is not None:
                haystack += " " + (mapping.provider_company_code or "").casefold()
                haystack += " " + (mapping.provider_company_name or "").casefold()
            if search_term not in haystack:
                continue
        rows.append(
            LocalCompanyProviderMappingOut(
                company_id=company.id,
                company_name=company.name,
                mapping=mapping,
            )
        )

    total = len(rows)
    offset = (params.page - 1) * params.size
    return Page.create(rows[offset : offset + params.size], params, total=total)


@router.get(
    "/provider-mappings/moabits/source-companies",
    response_model=Page[MoabitsSourceCompanyOut],
)
async def list_moabits_source_companies(
    current: AdminProfile,
    db: DbSession,
    params: PageParams,
    q: SearchQuery = None,
    active_only: bool = Query(default=True),
) -> Page[MoabitsSourceCompanyOut]:
    """List cached Moabits source companies discovered for the admin's credential source."""
    if current.company_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Profile is not scoped to a company",
        )
    result = await db.execute(
        _list_moabits_source_companies_query(
            current.company_id,
            q,
            active_only=active_only,
        )
    )
    rows = list(result.scalars().all())
    total = len(rows)
    offset = (params.page - 1) * params.size
    return Page.create(rows[offset : offset + params.size], params, total=total)


@router.get(
    "/provider-mappings/moabits/discover",
    response_model=MoabitsProviderMappingDiscoveryOut,
)
async def discover_moabits_provider_mappings(
    current: AdminProfile,
    db: DbSession,
    settings: SettingsDep,
) -> MoabitsProviderMappingDiscoveryOut:
    """Compare local companies with Moabits companies for an explicit link UI."""
    if current.company_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Profile is not scoped to a company",
        )
    credential = await _active_provider_credential(
        current.company_id,
        Provider.MOABITS,
        db,
    )
    if credential is None:
        raise HTTPException(status_code=404, detail="Moabits credential not found")

    local_result = await db.execute(select(Company).order_by(Company.name))
    local_companies = list(local_result.scalars().all())
    local_by_id = {company.id: company for company in local_companies}

    mapping_result = await db.execute(
        select(CompanyProviderMapping)
        .where(
            CompanyProviderMapping.provider == Provider.MOABITS.value,
            CompanyProviderMapping.active.is_(True),
        )
        .order_by(CompanyProviderMapping.provider_company_code)
    )
    mappings = list(mapping_result.scalars().all())
    mappings_by_company_id = {mapping.company_id: mapping for mapping in mappings}
    mappings_by_code: dict[str, list[CompanyProviderMapping]] = {}
    for mapping in mappings:
        mappings_by_code.setdefault(mapping.provider_company_code, []).append(mapping)

    credentials = decrypt_credentials(
        credential.credentials_enc,
        require_fernet_key(settings),
    )
    provider_rows_by_code: dict[str, dict[str, Any]] = {}
    for item in await fetch_child_companies(credentials):
        row = _moabits_company_row(item)
        if row is not None:
            provider_rows_by_code[row["company_code"]] = row
    provider_rows = list(provider_rows_by_code.values())
    await _cache_moabits_source_companies(current.company_id, provider_rows, db)
    source_company_codes = sorted({row["company_code"] for row in provider_rows})

    moabits_companies: list[MoabitsProviderCompanyOut] = []
    for row in provider_rows:
        linked_companies: list[MoabitsLinkedCompanyOut] = []
        for mapping in mappings_by_code.get(row["company_code"], []):
            linked_company = local_by_id.get(mapping.company_id)
            linked_companies.append(
                MoabitsLinkedCompanyOut(
                    company_id=mapping.company_id,
                    company_name=linked_company.name if linked_company else "",
                )
            )
        moabits_companies.append(
            MoabitsProviderCompanyOut(
                company_code=row["company_code"],
                company_name=row["company_name"],
                clie_id=row["clie_id"],
                selected_in_source=row["company_code"] in source_company_codes,
                linked_companies=linked_companies,
            )
        )

    moabits_companies.sort(
        key=lambda company: (
            company.company_name.casefold(),
            company.company_code,
        )
    )

    return MoabitsProviderMappingDiscoveryOut(
        cache_message=MOABITS_DISCOVERY_CACHE_MESSAGE,
        source_company_codes=source_company_codes,
        local_companies=[
            LocalCompanyProviderMappingOut(
                company_id=company.id,
                company_name=company.name,
                mapping=mappings_by_company_id.get(company.id),
            )
            for company in local_companies
        ],
        moabits_companies=moabits_companies,
    )


@router.get("/{company_id}", response_model=CompanyOut)
async def get_company(
    company_id: uuid.UUID,
    current: AdminProfile,
    db: DbSession,
) -> Company:
    """Get a company by ID. Admin only."""
    return await _get_company_or_404(company_id, db)


@router.put(
    "/{company_id}/provider-mappings/{provider}",
    response_model=CompanyProviderMappingOut,
)
async def upsert_company_provider_mapping(
    company_id: uuid.UUID,
    provider: Provider,
    body: CompanyProviderMappingUpdate,
    current: AdminProfile,
    db: DbSession,
    settings: SettingsDep,
) -> CompanyProviderMapping:
    """Create or update the Moabits mapping for a company. Validates the company code against the Moabits API. Admin only."""
    if provider != Provider.MOABITS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only Moabits company mappings are supported",
        )
    await _get_company_or_404(company_id, db)
    provider_company_code = body.provider_company_code.strip()
    if not provider_company_code:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="provider_company_code is required",
        )
    discovered_company = await _validate_moabits_mapping_code(
        provider_company_code,
        current.company_id,
        db,
        settings,
    )
    provider_company_name = (
        body.provider_company_name.strip()
        if body.provider_company_name is not None
        else None
    )
    if not provider_company_name and discovered_company is not None:
        provider_company_name = str(discovered_company.get("companyName") or "").strip()
        if not provider_company_name:
            provider_company_name = str(
                discovered_company.get("company_name") or ""
            ).strip()
    clie_id = body.clie_id
    if clie_id is None and discovered_company is not None:
        raw_clie_id = discovered_company.get(
            "clie_id", discovered_company.get("clieId")
        )
        clie_id = raw_clie_id if isinstance(raw_clie_id, int) else None

    now = datetime.now(UTC)
    mapping = await _get_mapping(company_id, provider, db)
    if mapping is None:
        mapping = CompanyProviderMapping(
            company_id=company_id,
            provider=provider.value,
            provider_company_code=provider_company_code,
            provider_company_name=provider_company_name,
            clie_id=clie_id,
            settings=body.settings,
            active=True,
            created_at=now,
            updated_at=now,
        )
        db.add(mapping)
    else:
        mapping.provider_company_code = provider_company_code
        mapping.provider_company_name = provider_company_name
        mapping.clie_id = clie_id
        mapping.settings = body.settings
        mapping.active = True
        mapping.updated_at = now

    await db.commit()
    await db.refresh(mapping)
    return mapping


@router.delete(
    "/{company_id}/provider-mappings/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_company_provider_mapping(
    company_id: uuid.UUID,
    provider: Provider,
    current: AdminProfile,
    db: DbSession,
) -> None:
    """Deactivate the Moabits mapping for a company. Admin only."""
    if provider != Provider.MOABITS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Only Moabits company mappings are supported",
        )
    await _get_company_or_404(company_id, db)
    mapping = await _get_mapping(company_id, provider, db)
    if mapping is None or not mapping.active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Provider mapping not found",
        )
    mapping.active = False
    mapping.updated_at = datetime.now(UTC)
    await db.commit()


@router.put("/{company_id}", response_model=CompanyOut)
async def update_company(
    company_id: uuid.UUID,
    body: CompanyUpdate,
    current: AdminProfile,
    db: DbSession,
) -> Company:
    """Rename a company by ID. Admin only."""
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if not company:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Company not found"
        )
    name = normalize_company_name(body.name)
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="name cannot be empty",
        )
    if name.casefold() != company.name.casefold():
        await ensure_company_name_is_available(db, name, exclude_company_id=company.id)
    company.name = name
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Company name already exists",
        ) from None

    await db.refresh(company)
    return company


@router.delete("/{company_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_company(
    company_id: uuid.UUID,
    current: AdminProfile,
    db: DbSession,
) -> None:
    """Delete a company and all its associated data. Admin only. Cannot delete own company."""
    if current.company_id == company_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own company",
        )
    await _get_company_or_404(company_id, db)

    profile_ids_result = await db.execute(
        select(Profile.id).where(Profile.company_id == company_id)
    )
    profile_ids = list(profile_ids_result.scalars().all())
    if profile_ids:
        await db.execute(delete(User).where(User.id.in_(profile_ids)))

    await db.execute(delete(Company).where(Company.id == company_id))
    await db.commit()


@router.get("/me/settings", response_model=CompanySettingsOut)
async def get_my_settings(
    current: CompanyProfile,
    db: DbSession,
) -> CompanySettings:
    """Get settings for the current user's company. Any authenticated role."""
    result = await db.execute(
        select(CompanySettings).where(CompanySettings.company_id == current.company_id)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Settings not found"
        )
    return row


@router.put("/me/settings", response_model=CompanySettingsOut)
async def update_my_settings(
    body: CompanySettingsUpdate,
    current: AdminProfile,
    db: DbSession,
) -> CompanySettings:
    """Update settings for the current user's company. Admin only."""
    result = await db.execute(
        select(CompanySettings).where(CompanySettings.company_id == current.company_id)
    )
    row = result.scalar_one_or_none()
    if row:
        row.settings = body.settings
        row.updated_at = datetime.now(UTC)
    else:
        row = CompanySettings(company_id=current.company_id, settings=body.settings)
        db.add(row)
    await db.commit()
    await db.refresh(row)
    return row
