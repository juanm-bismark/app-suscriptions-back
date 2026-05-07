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
from app.providers.moabits.adapter import fetch_child_companies
from app.providers.registry import ProviderRegistry
from app.shared.crypto import decrypt_credentials, encrypt_credentials
from app.shared.errors import DomainError
from app.tenancy.credential_expiry import credential_expiry_status
from app.tenancy.models.company import Company
from app.tenancy.models.credentials import CompanyProviderCredentials
from app.tenancy.models.provider_source_config import ProviderSourceConfig
from app.tenancy.schemas.credentials import (
    PROVIDER_CREDENTIAL_EXAMPLES,
    CredentialMetadataOut,
    CredentialTestOut,
    CredentialUpsertIn,
    MoabitsCompanyDiscoveryOut,
    MoabitsCompanyOut,
    MoabitsCompanySelectionIn,
)

router = APIRouter(prefix="/companies/me/credentials", tags=["credentials"])
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


async def _provider_source_config(
    provider: Provider,
    db: AsyncSession,
) -> ProviderSourceConfig | None:
    result = await db.execute(
        select(ProviderSourceConfig).where(
            ProviderSourceConfig.provider == provider.value,
        )
    )
    return result.scalar_one_or_none()


async def _current_company(company_id: UUID | None, db: AsyncSession) -> Company:
    if company_id is None:
        raise HTTPException(status_code=404, detail="Company not found")
    result = await db.execute(select(Company).where(Company.id == company_id))
    company = result.scalar_one_or_none()
    if company is None:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def _decrypt_active_credentials(
    row: CompanyProviderCredentials,
    settings: Settings,
) -> dict[str, Any]:
    fernet_key = _require_fernet_key(settings)
    return decrypt_credentials(row.credentials_enc, fernet_key)


def _selected_company_codes(credentials: dict[str, Any]) -> list[str]:
    raw_codes = credentials.get("company_codes", [])
    if isinstance(raw_codes, str):
        raw_codes = [raw_codes]
    if not isinstance(raw_codes, list):
        return []

    selected_codes: list[str] = []
    seen_codes: set[str] = set()
    for raw_code in raw_codes:
        code = str(raw_code).strip()
        if code and code not in seen_codes:
            selected_codes.append(code)
            seen_codes.add(code)
    return selected_codes


def _source_config_settings(row: ProviderSourceConfig | None) -> dict[str, Any]:
    if row is None:
        return {}
    return row.settings or {}


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


@router.get(
    "/moabits/companies/discover",
    response_model=MoabitsCompanyDiscoveryOut,
)
async def discover_moabits_companies(
    current: ManagerOrAdminProfile,
    db: DbSession,
    settings: SettingsDep,
) -> MoabitsCompanyDiscoveryOut:
    row = await _active_credential(current.company_id, Provider.MOABITS, db)
    if row is None:
        raise HTTPException(status_code=404, detail="Moabits credential not found")
    local_company = await _current_company(current.company_id, db)
    credentials = _decrypt_active_credentials(row, settings)
    source_config = await _provider_source_config(Provider.MOABITS, db)
    selected_company_codes = _selected_company_codes(
        _source_config_settings(source_config)
    )
    selected_company_code_set = set(selected_company_codes)

    companies: list[MoabitsCompanyOut] = []
    selected_companies: list[MoabitsCompanyOut] = []
    for item in await fetch_child_companies(credentials):
        company_code = str(item.get("companyCode") or "").strip()
        company_name = str(item.get("companyName") or "").strip()
        if not company_code or not company_name:
            continue
        clie_id = item.get("clie_id", item.get("clieId"))
        company = MoabitsCompanyOut(
            company_code=company_code,
            company_name=company_name,
            clie_id=clie_id if isinstance(clie_id, int) else None,
        )
        companies.append(company)
        if company_code in selected_company_code_set:
            selected_companies.append(company)

    companies.sort(
        key=lambda company: (
            company.company_name.casefold(),
            company.company_code,
        )
    )
    return MoabitsCompanyDiscoveryOut(
        current_company_name=local_company.name,
        selected_company_codes=selected_company_codes,
        selected_companies=selected_companies,
        companies=companies,
    )


@router.put("/moabits/company-codes", response_model=CredentialMetadataOut)
async def select_moabits_company_codes(
    body: MoabitsCompanySelectionIn,
    current: AdminProfile,
    db: DbSession,
    settings: SettingsDep,
) -> CredentialMetadataOut:
    row = await _active_credential(current.company_id, Provider.MOABITS, db)
    if row is None:
        raise HTTPException(status_code=404, detail="Moabits credential not found")
    fernet_key = _require_fernet_key(settings)
    credentials = decrypt_credentials(row.credentials_enc, fernet_key)
    requested_codes = [
        code for item in body.company_codes if (code := item.company_code.strip())
    ]
    if not requested_codes:
        raise HTTPException(
            status_code=422,
            detail="At least one Moabits company code is required",
        )

    provider_codes = {
        str(item.get("companyCode") or "").strip()
        for item in await fetch_child_companies(credentials)
    }
    missing_codes = sorted(set(requested_codes) - provider_codes)
    if missing_codes:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Some Moabits company codes are not available to this X-API-KEY",
                "company_codes": missing_codes,
            },
        )

    now = datetime.now(UTC)
    source_config = await _provider_source_config(Provider.MOABITS, db)
    if source_config is None:
        source_config = ProviderSourceConfig(
            provider=Provider.MOABITS.value,
            settings={"company_codes": requested_codes},
            updated_at=now,
            created_at=now,
        )
        db.add(source_config)
    else:
        source_config.settings = {
            **(source_config.settings or {}),
            "company_codes": requested_codes,
        }
        source_config.updated_at = now

    await db.commit()
    await db.refresh(source_config)
    return _metadata(row)


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
