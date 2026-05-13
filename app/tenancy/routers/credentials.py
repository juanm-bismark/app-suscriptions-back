import base64
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlparse
from uuid import UUID

from cryptography.hazmat.primitives.serialization.pkcs12 import (
    load_key_and_certificates,
)
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from sqlalchemy import String, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.config import Settings, get_settings
from app.database import get_db
from app.identity.dependencies import require_roles
from app.identity.models.profile import AppRole, Profile
from app.providers.base import Provider, SearchableProvider
from app.providers.registry import ProviderRegistry
from app.shared.crypto import decrypt_credentials, encrypt_credentials
from app.shared.errors import DomainError
from app.tenancy.credential_expiry import credential_expiry_status
from app.tenancy.models.company import Company
from app.tenancy.models.credentials import CompanyProviderCredentials
from app.tenancy.models.provider_mapping import CompanyProviderMapping
from app.tenancy.schemas.credentials import (
    PROVIDER_CREDENTIAL_EXAMPLES,
    AdminCredentialMetadataOut,
    CredentialMetadataOut,
    CredentialPatchIn,
    CredentialTestOut,
    CredentialUpsertIn,
)

router = APIRouter(prefix="/companies/me/credentials", tags=["credentials"])
admin_credentials_router = APIRouter(
    prefix="/admin/credentials",
    tags=["admin-credentials"],
)
admin_company_credentials_router = APIRouter(
    prefix="/admin/companies/{company_id}/credentials",
    tags=["admin-credentials"],
)
TELE2_DEFAULT_COBRAND_HOST = "restapi3.jasper.com"


def get_registry(request: Request) -> ProviderRegistry:
    registry: ProviderRegistry = request.app.state.provider_registry
    return registry


ManagerOrAdminProfile = Annotated[
    Profile,
    Depends(require_roles(AppRole.manager, AppRole.admin)),
]
AdminProfile = Annotated[Profile, Depends(require_roles(AppRole.admin))]
DbSession = Annotated[AsyncSession, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RegistryDep = Annotated[ProviderRegistry, Depends(get_registry)]
SearchQuery = Annotated[str | None, Query()]
CredentialBody = Annotated[
    CredentialUpsertIn,
    Body(openapi_examples=PROVIDER_CREDENTIAL_EXAMPLES),
]
CredentialPatchBody = Annotated[
    CredentialPatchIn,
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


def _admin_metadata(row: CompanyProviderCredentials) -> AdminCredentialMetadataOut:
    return AdminCredentialMetadataOut(
        company_id=row.company_id,
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


def _normalized_kite_credentials(body: CredentialUpsertIn) -> dict[str, Any]:
    credentials = dict(body.credentials)
    endpoint = str(credentials.get("endpoint") or "").strip().rstrip("/")
    if not endpoint:
        raise ValueError("Kite endpoint is required")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Kite endpoint must be a valid HTTPS URL")
    credentials["endpoint"] = endpoint

    pfx_b64 = credentials.get("client_cert_pfx_b64") or credentials.get("pfx_base64")
    if not pfx_b64:
        raise ValueError("Kite client_cert_pfx_b64 is required")
    credentials["client_cert_pfx_b64"] = pfx_b64
    credentials.pop("pfx_base64", None)

    pfx_password = credentials.get("client_cert_password") or credentials.get(
        "pfx_password"
    )
    if not pfx_password:
        raise ValueError("Kite client_cert_password is required")
    credentials["client_cert_password"] = pfx_password
    credentials.pop("pfx_password", None)

    username = credentials.get("username")
    password = credentials.get("password")
    if bool(username) != bool(password):
        raise ValueError(
            "Kite WS-Security credentials require both username and password"
        )

    ca_bundle_b64 = (
        credentials.get("server_ca_bundle_pem_b64")
        or credentials.get("server_ca_cert_pem_b64")
        or credentials.get("ca_bundle_pem_b64")
        or credentials.get("ca_cert_pem_b64")
    )
    if ca_bundle_b64:
        credentials["server_ca_bundle_pem_b64"] = ca_bundle_b64
        for alias in (
            "server_ca_cert_pem_b64",
            "ca_bundle_pem_b64",
            "ca_cert_pem_b64",
        ):
            credentials.pop(alias, None)

    ca_bundle_pem = (
        credentials.get("server_ca_bundle_pem")
        or credentials.get("server_ca_cert_pem")
        or credentials.get("ca_bundle_pem")
        or credentials.get("ca_cert_pem")
    )
    if ca_bundle_pem:
        credentials["server_ca_bundle_pem"] = ca_bundle_pem
        for alias in ("server_ca_cert_pem", "ca_bundle_pem", "ca_cert_pem"):
            credentials.pop(alias, None)

    if body.account_scope.get("end_customer_id") is not None:
        credentials["end_customer_id"] = body.account_scope["end_customer_id"]
    if body.account_scope.get("environment") is not None:
        credentials["environment"] = body.account_scope["environment"]
    return credentials


def _normalized_credentials(
    provider: Provider, body: CredentialUpsertIn
) -> dict[str, Any]:
    credentials = dict(body.credentials)
    if provider == Provider.KITE:
        return _normalized_kite_credentials(body)
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


def _format_expiry_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _kite_certificate_expires_at(credentials: dict[str, Any]) -> datetime:
    try:
        pfx = base64.b64decode(credentials["client_cert_pfx_b64"])
        password = credentials.get("client_cert_password")
        password_bytes = str(password).encode("utf-8") if password else None
        _private_key, certificate, _additional_certs = load_key_and_certificates(
            pfx, password_bytes
        )
    except Exception as exc:
        raise ValueError(f"Kite client certificate could not be loaded: {exc}") from exc

    if certificate is None:
        raise ValueError("Kite client certificate PFX is missing a certificate")
    return certificate.not_valid_after_utc


def _normalized_upsert_body(
    provider: Provider,
    body: CredentialUpsertIn,
) -> CredentialUpsertIn:
    credentials = _normalized_credentials(provider, body)
    account_scope = dict(body.account_scope)
    if provider == Provider.KITE:
        account_scope["cert_expires_at"] = _format_expiry_datetime(
            _kite_certificate_expires_at(credentials)
        )
    return CredentialUpsertIn(credentials=credentials, account_scope=account_scope)


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


def _credential_search_clause(q: str):
    pattern = f"%{q.strip()}%"
    return or_(
        CompanyProviderCredentials.provider.ilike(pattern),
        cast(CompanyProviderCredentials.account_scope, String).ilike(pattern),
    )


def _list_credentials_query(
    company_id: UUID | None,
    q: str | None = None,
) -> Select[tuple[CompanyProviderCredentials]]:
    query = (
        select(CompanyProviderCredentials)
        .where(
            CompanyProviderCredentials.company_id == company_id,
            CompanyProviderCredentials.active.is_(True),
        )
        .order_by(CompanyProviderCredentials.provider)
    )
    if q and q.strip():
        query = query.where(_credential_search_clause(q))
    return query


def _admin_list_all_credentials_query(
    q: str | None = None,
) -> Select[tuple[CompanyProviderCredentials]]:
    query = select(CompanyProviderCredentials).where(
        CompanyProviderCredentials.active.is_(True)
    )
    if q and (term := q.strip()):
        pattern = f"%{term}%"
        query = query.outerjoin(
            Company,
            Company.id == CompanyProviderCredentials.company_id,
        ).where(
            or_(
                _credential_search_clause(term),
                Company.name.ilike(pattern),
            )
        )
    return query.order_by(
        CompanyProviderCredentials.company_id,
        CompanyProviderCredentials.provider,
    )


async def _current_company(company_id: UUID | None, db: AsyncSession) -> Company:
    if company_id is None:
        raise HTTPException(status_code=404, detail="Company not found")
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


async def _ensure_company_exists(company_id: UUID, db: AsyncSession) -> None:
    await _current_company(company_id, db)


def _coerce_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


async def _live_test_credentials(
    provider: Provider,
    body: CredentialUpsertIn,
    company_id: UUID | None,
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
        normalized_body = _normalized_upsert_body(provider, body)
    except ValueError as exc:
        return CredentialTestOut(provider=provider.value, ok=False, detail=str(exc))
    credentials = dict(normalized_body.credentials)
    credentials["company_id"] = str(company_id) if company_id is not None else ""
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


async def _persist_credential(
    company_id: UUID | None,
    provider: Provider,
    body: CredentialUpsertIn,
    db: AsyncSession,
    settings: Settings,
    registry: ProviderRegistry,
) -> CompanyProviderCredentials:
    fernet_key = _require_fernet_key(settings)
    try:
        normalized_body = _normalized_upsert_body(provider, body)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=str(exc),
        ) from exc
    test_result = await _live_test_credentials(
        provider, normalized_body, company_id, registry
    )
    if not test_result.ok:
        raise HTTPException(
            status_code=422,
            detail=test_result.detail or "Credential test failed",
        )

    now = datetime.now(UTC)
    encrypted_credentials = encrypt_credentials(
        normalized_body.credentials,
        fernet_key,
    )
    row = await _active_credential(company_id, provider, db)
    if row is None:
        row = CompanyProviderCredentials(
            company_id=company_id,
            provider=provider.value,
            credentials_enc=encrypted_credentials,
            account_scope=normalized_body.account_scope,
            active=True,
            rotated_at=now,
            created_at=now,
        )
        db.add(row)
    else:
        row.credentials_enc = encrypted_credentials
        row.account_scope = normalized_body.account_scope
        row.active = True
        row.rotated_at = now
    await db.commit()
    await db.refresh(row)
    return row


def _patch_credentials(body: CredentialPatchIn) -> dict[str, Any]:
    credentials = dict(body.credentials)
    if "x-api-key" in credentials and "x_api_key" not in credentials:
        credentials["x_api_key"] = credentials.pop("x-api-key")
    return credentials


def _ensure_patch_has_fields(body: CredentialPatchIn) -> None:
    if body.credentials or body.account_scope is not None:
        return
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="PATCH body must include credentials or account_scope",
    )



async def _active_moabits_mapping(
    company_id: UUID | None,
    db: AsyncSession,
) -> CompanyProviderMapping | None:
    if company_id is None:
        return None
    result = await db.execute(
        select(CompanyProviderMapping).where(
            CompanyProviderMapping.company_id == company_id,
            CompanyProviderMapping.provider == Provider.MOABITS.value,
            CompanyProviderMapping.active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def _persist_credential_patch(
    company_id: UUID | None,
    provider: Provider,
    body: CredentialPatchIn,
    db: AsyncSession,
    settings: Settings,
    registry: ProviderRegistry,
    *,
    allow_create: bool,
) -> CompanyProviderCredentials:
    _ensure_patch_has_fields(body)
    fernet_key = _require_fernet_key(settings)
    row = await _active_credential(company_id, provider, db)
    if row is None and not allow_create:
        raise HTTPException(status_code=404, detail="Credential not found")

    current_credentials = (
        decrypt_credentials(row.credentials_enc, fernet_key)
        if row is not None
        else {}
    )
    merged_credentials = {
        **current_credentials,
        **_patch_credentials(body),
    }
    merged_account_scope = dict(row.account_scope or {}) if row is not None else {}
    if body.account_scope is not None:
        merged_account_scope.update(body.account_scope)

    return await _persist_credential(
        company_id,
        provider,
        CredentialUpsertIn(
            credentials=merged_credentials,
            account_scope=merged_account_scope,
        ),
        db,
        settings,
        registry,
    )


@router.get("", response_model=list[CredentialMetadataOut])
async def list_credentials(
    current: ManagerOrAdminProfile,
    db: DbSession,
    q: SearchQuery = None,
) -> list[CredentialMetadataOut]:
    """List active credential metadata for the current company. Secrets are never returned. Manager or admin."""
    result = await db.execute(_list_credentials_query(current.company_id, q))
    return [_metadata(row) for row in result.scalars().all()]


@router.get("/{provider}", response_model=CredentialMetadataOut)
async def get_credential(
    provider: Provider,
    current: ManagerOrAdminProfile,
    db: DbSession,
) -> CredentialMetadataOut:
    """Get metadata for a specific provider credential. Secrets are never returned. Manager or admin."""
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
    """Test candidate credentials against the provider API without persisting. Manager or admin."""
    return await _live_test_credentials(provider, body, current.company_id, registry)


@router.patch("/{provider}", response_model=CredentialMetadataOut)
async def rotate_credential(
    provider: Provider,
    body: CredentialPatchBody,
    current: ManagerOrAdminProfile,
    db: DbSession,
    settings: SettingsDep,
    registry: RegistryDep,
) -> CredentialMetadataOut:
    """Rotate (replace or merge-update) credentials for own company. Managers may update any provider but cannot create credentials. For Moabits, managers may only update x_api_key and the company must have an active provider mapping."""
    is_admin = current.role == AppRole.admin
    if not is_admin and provider == Provider.MOABITS:
        credentials = _patch_credentials(body)
        if set(credentials) != {"x_api_key"} or body.account_scope not in (None, {}):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Managers can only update Moabits x_api_key",
            )
        mapping = await _active_moabits_mapping(current.company_id, db)
        if mapping is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Company is not linked to a Moabits company code",
            )
    row = await _persist_credential_patch(
        current.company_id,
        provider,
        body,
        db,
        settings,
        registry,
        allow_create=is_admin,
    )
    return _metadata(row)


@router.delete("/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_credential(
    provider: Provider,
    current: AdminProfile,
    db: DbSession,
) -> None:
    """Deactivate a provider credential. Admin only."""
    row = await _active_credential(current.company_id, provider, db)
    if row is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    row.active = False
    await db.commit()


@admin_credentials_router.get("", response_model=list[AdminCredentialMetadataOut])
async def admin_list_all_credentials(
    current: AdminProfile,
    db: DbSession,
    q: SearchQuery = None,
) -> list[AdminCredentialMetadataOut]:
    """List all active credentials across all companies. Secrets are never returned. Admin only."""
    result = await db.execute(_admin_list_all_credentials_query(q))
    return [_admin_metadata(row) for row in result.scalars().all()]


@admin_company_credentials_router.get(
    "",
    response_model=list[AdminCredentialMetadataOut],
)
async def admin_list_company_credentials(
    company_id: UUID,
    current: AdminProfile,
    db: DbSession,
    q: SearchQuery = None,
) -> list[AdminCredentialMetadataOut]:
    """List active credentials for a specific company. Secrets are never returned. Admin only."""
    await _ensure_company_exists(company_id, db)
    result = await db.execute(_list_credentials_query(company_id, q))
    return [_admin_metadata(row) for row in result.scalars().all()]


@admin_company_credentials_router.get(
    "/{provider}",
    response_model=AdminCredentialMetadataOut,
)
async def admin_get_company_credential(
    company_id: UUID,
    provider: Provider,
    current: AdminProfile,
    db: DbSession,
) -> AdminCredentialMetadataOut:
    """Get credential metadata for a specific company and provider. Admin only."""
    await _ensure_company_exists(company_id, db)
    row = await _active_credential(company_id, provider, db)
    if row is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    return _admin_metadata(row)


@admin_company_credentials_router.post(
    "/{provider}/test",
    response_model=CredentialTestOut,
)
async def admin_test_company_credential(
    company_id: UUID,
    provider: Provider,
    body: CredentialBody,
    current: AdminProfile,
    db: DbSession,
    registry: RegistryDep,
) -> CredentialTestOut:
    """Test candidate credentials for a specific company against the provider API without persisting. Admin only."""
    await _ensure_company_exists(company_id, db)
    return await _live_test_credentials(provider, body, company_id, registry)


@admin_company_credentials_router.patch(
    "/{provider}",
    response_model=AdminCredentialMetadataOut,
)
async def admin_rotate_company_credential(
    company_id: UUID,
    provider: Provider,
    body: CredentialPatchBody,
    current: AdminProfile,
    db: DbSession,
    settings: SettingsDep,
    registry: RegistryDep,
) -> AdminCredentialMetadataOut:
    """Rotate credentials for a specific company and provider. Admin only."""
    await _ensure_company_exists(company_id, db)
    row = await _persist_credential_patch(
        company_id,
        provider,
        body,
        db,
        settings,
        registry,
        allow_create=True,
    )
    return _admin_metadata(row)


@admin_company_credentials_router.delete(
    "/{provider}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def admin_deactivate_company_credential(
    company_id: UUID,
    provider: Provider,
    current: AdminProfile,
    db: DbSession,
) -> None:
    """Deactivate a credential for a specific company and provider. Admin only."""
    await _ensure_company_exists(company_id, db)
    row = await _active_credential(company_id, provider, db)
    if row is None:
        raise HTTPException(status_code=404, detail="Credential not found")
    row.active = False
    await db.commit()
