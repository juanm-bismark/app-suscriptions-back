from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlparse
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_db
from app.identity.dependencies import require_roles
from app.identity.models.profile import AppRole, Profile
from app.providers.base import Provider, SearchableProvider
from app.providers.registry import ProviderRegistry
from app.shared.crypto import encrypt_credentials
from app.shared.errors import DomainError
from app.tenancy.credential_expiry import credential_expiry_status
from app.tenancy.models.credentials import CompanyProviderCredentials
from app.tenancy.schemas.credentials import (
    PROVIDER_CREDENTIAL_EXAMPLES,
    CredentialMetadataOut,
    CredentialTestOut,
    CredentialUpsertIn,
)

router = APIRouter(prefix="/companies/me/credentials", tags=["credentials"])
TELE2_DEFAULT_COBRAND_HOST = "restapi3.jasper.com"


def get_registry(request: Request) -> ProviderRegistry:
    return request.app.state.provider_registry


ManagerOrAdminProfile = Annotated[
    Profile,
    Depends(require_roles(AppRole.manager, AppRole.admin)),
]
AdminProfile = Annotated[Profile, Depends(require_roles(AppRole.admin))]
DbSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_registry)]
CredentialBody = Annotated[
    CredentialUpsertIn,
    Body(openapi_examples=PROVIDER_CREDENTIAL_EXAMPLES),
]


def _require_fernet_key(settings: Settings) -> str:
    if not settings.fernet_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Missing FERNET_KEY configuration",
        )
    return settings.fernet_key


def _metadata(row: CompanyProviderCredentials) -> CredentialMetadataOut:
    return CredentialMetadataOut(
        provider=row.provider,
        active=row.active,
        rotated_at=row.rotated_at,
        created_at=row.created_at,
        account_scope=row.account_scope or {},
        expiry_status=credential_expiry_status(row.account_scope),
    )


def _tele2_base_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    if not raw:
        raise ValueError("Tele2 cobrand_url cannot be empty")
    with_scheme = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
    parsed = urlparse(with_scheme)
    if not parsed.netloc:
        raise ValueError("Tele2 cobrand_url must be a valid host")
    return f"{parsed.scheme}://{parsed.netloc}"


def _normalized_credentials(provider: Provider, body: CredentialUpsertIn) -> dict[str, Any]:
    credentials = dict(body.credentials)
    if provider != Provider.TELE2:
        return credentials

    cobrand_url = credentials.pop("cobrand_url", None)
    base_url = credentials.get("base_url")
    if cobrand_url:
        credentials["base_url"] = _tele2_base_url(str(cobrand_url))
    elif base_url:
        credentials["base_url"] = _tele2_base_url(str(base_url))
    else:
        credentials["base_url"] = _tele2_base_url(TELE2_DEFAULT_COBRAND_HOST)

    if "account_id" not in credentials and body.account_scope.get("account_id"):
        credentials["account_id"] = body.account_scope["account_id"]
    if body.account_scope.get("max_tps") is not None:
        credentials["max_tps"] = body.account_scope["max_tps"]
    return credentials


async def _active_credential(
    company_id: UUID | None,
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


async def _live_test_credentials(
    provider: Provider,
    body: CredentialUpsertIn,
    current: Profile,
    registry: ProviderRegistry,
) -> CredentialTestOut:
    adapter = registry.get(provider.value)
    if not isinstance(adapter, SearchableProvider):
        return CredentialTestOut(
            provider=provider.value,
            ok=False,
            detail=f"Provider '{provider.value}' does not support credential testing",
        )

    try:
        credentials = _normalized_credentials(provider, body)
    except ValueError as exc:
        return CredentialTestOut(provider=provider.value, ok=False, detail=str(exc))
    credentials["company_id"] = str(current.company_id)
    cursor = None
    if provider == Provider.TELE2:
        since = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
        cursor = f"page:1|since:{since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    try:
        await adapter.list_subscriptions(
            credentials,
            cursor=cursor,
            limit=1,
            filters=None,
        )
    except DomainError as exc:
        return CredentialTestOut(
            provider=provider.value,
            ok=False,
            detail=exc.detail or exc.title,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return CredentialTestOut(
            provider=provider.value,
            ok=False,
            detail=str(exc),
        )

    return CredentialTestOut(provider=provider.value, ok=True, detail=None)


@router.get("", response_model=list[CredentialMetadataOut])
async def list_credentials(
    current: ManagerOrAdminProfile,
    db: DbSession,
) -> list[CredentialMetadataOut]:
    result = await db.execute(
        select(CompanyProviderCredentials)
        .where(
            CompanyProviderCredentials.company_id == current.company_id,
            CompanyProviderCredentials.active.is_(True),
        )
        .order_by(CompanyProviderCredentials.provider)
    )
    return [_metadata(row) for row in result.scalars().all()]


@router.get("/{provider}", response_model=CredentialMetadataOut)
async def get_credential(
    provider: Provider,
    current: ManagerOrAdminProfile,
    db: DbSession,
) -> CredentialMetadataOut:
    row = await _active_credential(current.company_id, provider, db)
    if row is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    return _metadata(row)


@router.post("/{provider}/test", response_model=CredentialTestOut)
async def test_credential(
    provider: Provider,
    body: CredentialBody,
    current: ManagerOrAdminProfile,
    registry: RegistryDep,
) -> CredentialTestOut:
    return await _live_test_credentials(provider, body, current, registry)


@router.patch("/{provider}", response_model=CredentialMetadataOut)
async def rotate_credential(
    provider: Provider,
    body: CredentialBody,
    current: ManagerOrAdminProfile,
    db: DbSession,
    settings: SettingsDep,
    registry: RegistryDep,
) -> CredentialMetadataOut:
    fernet_key = _require_fernet_key(settings)
    test_result = await _live_test_credentials(provider, body, current, registry)
    if not test_result.ok:
        raise HTTPException(
            status_code=422,
            detail=test_result.detail or "Credential test failed",
        )

    now = datetime.now(UTC)
    await db.execute(
        update(CompanyProviderCredentials)
        .where(
            CompanyProviderCredentials.company_id == current.company_id,
            CompanyProviderCredentials.provider == provider.value,
            CompanyProviderCredentials.active.is_(True),
        )
        .values(active=False)
    )
    row = CompanyProviderCredentials(
        company_id=current.company_id,
        provider=provider.value,
        credentials_enc=encrypt_credentials(
            _normalized_credentials(provider, body),
            fernet_key,
        ),
        account_scope=body.account_scope,
        active=True,
        rotated_at=now,
        created_at=now,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _metadata(row)


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_credential(
    provider: Provider,
    current: AdminProfile,
    db: DbSession,
) -> None:
    row = await _active_credential(current.company_id, provider, db)
    if row is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    row.active = False
    await db.commit()
