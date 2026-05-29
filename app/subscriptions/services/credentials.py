"""Provider credential loading and admin credential helpers."""
import dataclasses
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, require_fernet_key
from app.providers.base import Provider
from app.shared.crypto import decrypt_credentials
from app.shared.errors import CredentialsMissing, ListingPreconditionFailed
from app.subscriptions.domain import SubscriptionSearchFilters
from app.subscriptions.services.provider_dispatch import _adapter_bootstrap_filters
from app.tenancy.credential_expiry import (
    CredentialExpiryStatus,
    credential_expiry_datetime,
    credential_expiry_status,
)
from app.tenancy.models.credentials import CompanyProviderCredentials
from app.tenancy.models.provider_mapping import CompanyProviderMapping

logger = structlog.get_logger(__name__)


async def _load_credentials(
    company_id: uuid.UUID,
    provider: str,
    db: AsyncSession,
    settings: Settings,
) -> dict:
    result = await db.execute(
        select(CompanyProviderCredentials).where(
            CompanyProviderCredentials.company_id == company_id,
            CompanyProviderCredentials.provider == provider,
            CompanyProviderCredentials.active.is_(True),
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise CredentialsMissing(
            detail=f"No active credentials for provider '{provider}'"
        )
    _warn_if_kite_certificate_expiring(row)
    creds = decrypt_credentials(row.credentials_enc, require_fernet_key(settings))
    creds["company_id"] = str(company_id)
    creds["account_scope"] = row.account_scope or {}
    if row.provider == "tele2" and (row.account_scope or {}).get("max_tps") is not None:
        creds["max_tps"] = (row.account_scope or {})["max_tps"]
    if row.provider == Provider.MOABITS.value:
        mapping_result = await db.execute(
            select(CompanyProviderMapping).where(
                CompanyProviderMapping.company_id == company_id,
                CompanyProviderMapping.provider == Provider.MOABITS.value,
                CompanyProviderMapping.active.is_(True),
            )
        )
        mapping = mapping_result.scalar_one_or_none()
        if not isinstance(mapping, CompanyProviderMapping):
            raise ListingPreconditionFailed(
                detail=(
                    "Company is not linked to a Moabits company code. "
                    "An admin must configure the provider mapping first."
                ),
                extra={"provider": Provider.MOABITS.value},
            )
        creds["company_code"] = mapping.provider_company_code
        creds["provider_company_mapping"] = {
            "companyCode": mapping.provider_company_code,
            "companyName": mapping.provider_company_name,
            "clie_id": mapping.clie_id,
        }
    return creds


def _warn_if_kite_certificate_expiring(row: CompanyProviderCredentials) -> None:
    if row.provider != "kite":
        return
    expiry_status = credential_expiry_status(row.account_scope)
    if expiry_status == CredentialExpiryStatus.VALID:
        return
    expires_raw = (row.account_scope or {}).get("cert_expires_at")
    expires_at = credential_expiry_datetime(row.account_scope)
    if expiry_status == CredentialExpiryStatus.INVALID or expires_at is None:
        logger.warning(
            "kite_cert_expiry_invalid",
            company_id=str(row.company_id),
            credential_id=str(row.id),
            cert_expires_at=expires_raw,
        )
        return
    days_remaining = (expires_at - datetime.now(UTC)).days
    if days_remaining in {30, 15, 7} or days_remaining < 7:
        logger.warning(
            "kite_cert_expiring",
            company_id=str(row.company_id),
            credential_id=str(row.id),
            cert_expires_at=expires_at.isoformat(),
            days_remaining=days_remaining,
        )


async def _active_admin_credential_rows(
    db: AsyncSession,
    provider: Provider | None = None,
) -> list[CompanyProviderCredentials]:
    stmt = select(CompanyProviderCredentials).where(
        CompanyProviderCredentials.active.is_(True)
    )
    if provider is not None:
        stmt = stmt.where(CompanyProviderCredentials.provider == provider.value)
    result = await db.execute(
        stmt.order_by(
            CompanyProviderCredentials.provider,
            CompanyProviderCredentials.company_id,
        )
    )
    return list(result.scalars().all())


def _credential_row_provider(row: CompanyProviderCredentials) -> str:
    return str(row.provider)


def _credential_row_company_id(row: CompanyProviderCredentials) -> uuid.UUID:
    company_id = row.company_id
    if isinstance(company_id, uuid.UUID):
        return company_id
    return uuid.UUID(str(company_id))


def _admin_effective_filters(
    provider_name: str,
    adapter: Any,
    filters: SubscriptionSearchFilters,
    *,
    use_bootstrap: bool,
) -> SubscriptionSearchFilters:
    if not use_bootstrap:
        return filters
    bootstrap = _adapter_bootstrap_filters(provider_name, adapter)
    return dataclasses.replace(
        bootstrap,
        status=filters.status,
        modified_since=filters.modified_since or bootstrap.modified_since,
        modified_till=filters.modified_till,
        iccid=filters.iccid,
        imsi=filters.imsi,
        msisdn=filters.msisdn,
        imei=filters.imei,
        operator=filters.operator,
        data_service=filters.data_service,
        sms_service=filters.sms_service,
        last_lu_since=filters.last_lu_since,
        last_lu_till=filters.last_lu_till,
        imsi_list=filters.imsi_list,
        custom=filters.custom,
    )
