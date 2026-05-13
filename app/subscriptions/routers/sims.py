"""SIM management API endpoints.

All endpoints are scoped to the caller's company via their JWT profile.
The routing table (SimRoutingMap) maps iccid → provider + company_id and is
populated lazily on first successful list, or explicitly via POST /import.
"""

import asyncio
import base64
import dataclasses
import json
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings, require_fernet_key
from app.database import get_db
from app.identity.dependencies import (
    get_current_company_id,
    require_roles,
)
from app.identity.models.profile import AppRole, Profile
from app.providers.base import Provider
from app.providers.registry import ProviderRegistry
from app.shared.crypto import decrypt_credentials
from app.shared.errors import (
    CredentialsMissing,
    DomainError,
    IdempotencyKeyRequired,
    ListingPreconditionFailed,
    SubscriptionNotFound,
    UnsupportedOperation,
)
from app.subscriptions.domain import (
    AdministrativeStatus,
    Subscription,
    SubscriptionSearchFilters,
)
from app.subscriptions.models.lifecycle_audit import LifecycleChangeAudit
from app.subscriptions.models.routing import SimRoutingMap
from app.subscriptions.schemas.sim import (
    PresenceOut,
    ProviderStatusOut,
    SimImportIn,
    SimImportOut,
    SimListOut,
    StatusChangeIn,
    SubscriptionOut,
    UsageOut,
)
from app.tenancy.credential_expiry import (
    CredentialExpiryStatus,
    credential_expiry_datetime,
    credential_expiry_status,
)
from app.tenancy.models.credentials import CompanyProviderCredentials
from app.tenancy.models.idempotency import IdempotencyKey
from app.tenancy.models.provider_mapping import CompanyProviderMapping

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/sims", tags=["sims"])
_REQUEST_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_GLOBAL_PROVIDER_PAGE_SIZE = 25
_GLOBAL_CURSOR_PREFIX = "global:"


# ── Dependencies ────────────────────────────────────────────────────────────────


def get_registry(request: Request) -> ProviderRegistry:
    registry: ProviderRegistry = request.app.state.provider_registry
    return registry


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
        creds["company_codes"] = [mapping.provider_company_code]
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


async def _resolve_routing(
    iccid: str,
    company_id: uuid.UUID,
    db: AsyncSession,
) -> SimRoutingMap:
    routing = await _find_routing(iccid, company_id, db)
    if routing is None:
        raise SubscriptionNotFound(
            detail=(
                f"SIM {iccid} not found. Populate the routing index first via "
                "GET /v1/sims?provider=<name> (lazy discovery) or "
                "POST /v1/sims/import (bulk import)."
            )
        )
    return routing


async def _find_routing(
    iccid: str,
    company_id: uuid.UUID,
    db: AsyncSession,
) -> SimRoutingMap | None:
    result = await db.execute(
        select(SimRoutingMap).where(
            SimRoutingMap.iccid == iccid,
            SimRoutingMap.company_id == company_id,
        )
    )
    return result.scalar_one_or_none()


def _require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if idempotency_key is None:
        raise IdempotencyKeyRequired()
    return idempotency_key


def _to_out(sub: Subscription) -> SubscriptionOut:
    data = dataclasses.asdict(sub)
    data["detail_level"] = _detail_level(data)
    data["normalized"] = _normalized_subscription(data)
    return SubscriptionOut(**data)


def _iso_datetime(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _boolish(value: Any) -> bool | Any:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "enabled"}:
            return True
        if normalized in {"false", "0", "no", "disabled"}:
            return False
    return value


def _first_present(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None and value != "":
            return value
    return None


def _custom_fields(provider_fields: dict[str, Any]) -> dict[str, Any]:
    prefixes = (
        "account_custom_",
        "operator_custom_",
        "customer_custom_",
        "custom_field_",
    )
    return {
        key: value
        for key, value in provider_fields.items()
        if any(key.startswith(prefix) for prefix in prefixes)
    }


def _detail_level(data: dict[str, Any]) -> str:
    provider_fields = data.get("provider_fields") or {}
    if "detail_enriched" in provider_fields:
        return "detail" if provider_fields.get("detail_enriched") is True else "summary"
    return "detail"


def _normalized_subscription(data: dict[str, Any]) -> dict[str, Any]:
    provider_fields: dict[str, Any] = data.get("provider_fields") or {}
    services = provider_fields.get("services")
    if services is None and provider_fields.get("services_raw"):
        services = [
            item.strip().lower()
            for item in str(provider_fields["services_raw"]).split("/")
            if item.strip()
        ]

    return {
        "identity": {
            "iccid": data.get("iccid"),
            "msisdn": data.get("msisdn"),
            "imsi": data.get("imsi"),
            "imei": _first_present(provider_fields, "imei"),
            "alias": _first_present(provider_fields, "alias"),
            "eid": _first_present(provider_fields, "eid"),
            "euiccid": _first_present(provider_fields, "euiccid"),
            "sim_profile_id": _first_present(provider_fields, "sim_profile_id"),
        },
        "status": {
            "value": str(data.get("status")) if data.get("status") else None,
            "native": data.get("native_status"),
            "last_changed_at": _iso_datetime(
                _first_present(
                    provider_fields, "last_state_change_date", "date_updated"
                )
                or data.get("updated_at")
            ),
        },
        "plan": {
            "name": _first_present(
                provider_fields,
                "product_name",
                "rate_plan",
                "commercial_group",
            ),
            "code": _first_present(provider_fields, "product_code"),
            "id": _first_present(provider_fields, "product_id"),
            "communication_plan": _first_present(provider_fields, "communication_plan"),
            "apn": _first_present(provider_fields, "apn"),
            "apns": _first_present(provider_fields, "apn_list"),
            "started_at": _iso_datetime(
                _first_present(provider_fields, "plan_start_date")
            ),
            "expires_at": _iso_datetime(
                _first_present(provider_fields, "plan_expiration_date")
            ),
        },
        "customer": {
            "name": _first_present(
                provider_fields,
                "client_name",
                "end_customer_name",
                "customer",
            ),
            "id": _first_present(
                provider_fields,
                "end_customer_id",
                "end_consumer_id",
            ),
            "company_code": _first_present(provider_fields, "company_code"),
            "account_id": _first_present(provider_fields, "account_id"),
        },
        "network": {
            "operator": _first_present(provider_fields, "operator"),
            "country": _first_present(provider_fields, "country"),
            "rat_type": _first_present(provider_fields, "rat_type"),
            "last_network": _first_present(provider_fields, "last_network"),
            "ip_address": _first_present(
                provider_fields,
                "ip_address",
                "fixed_ip_address",
                "fixed_ipv6_address",
                "ipv6_address",
            ),
            "sgsn_ip": _first_present(provider_fields, "sgsn_ip"),
            "ggsn_ip": _first_present(provider_fields, "ggsn_ip"),
            "last_traffic_at": _iso_datetime(
                _first_present(provider_fields, "last_traffic_date")
            ),
            "first_lu_at": _iso_datetime(_first_present(provider_fields, "first_lu")),
            "last_lu_at": _iso_datetime(_first_present(provider_fields, "last_lu")),
            "first_cdr_at": _iso_datetime(_first_present(provider_fields, "first_cdr")),
            "last_cdr_at": _iso_datetime(_first_present(provider_fields, "last_cdr")),
            "gprs": _first_present(provider_fields, "gprs_status"),
            "ip": _first_present(provider_fields, "ip_status"),
            "location": _first_present(
                provider_fields, "automatic_location", "manual_location"
            ),
        },
        "hardware": {
            "sim_model": _first_present(provider_fields, "sim_model"),
            "module_manufacturer": _first_present(
                provider_fields, "comm_module_manufacturer"
            ),
            "module_model": _first_present(provider_fields, "comm_module_model"),
            "device_id": _first_present(provider_fields, "device_id"),
            "modem_id": _first_present(provider_fields, "modem_id"),
            "imei_last_changed_at": _iso_datetime(
                _first_present(provider_fields, "imei_last_change")
            ),
            "shipped_at": _iso_datetime(
                _first_present(provider_fields, "date_shipped")
            ),
        },
        "services": {
            "active": services,
            "basic": _first_present(provider_fields, "basic_services"),
            "supplementary": _first_present(provider_fields, "supplementary_services"),
            "data_service": _boolish(_first_present(provider_fields, "data_service")),
            "sms_service": _boolish(_first_present(provider_fields, "sms_service")),
        },
        "limits": {
            "data": _first_present(provider_fields, "data_limit_mb"),
            "data_unit": "mb"
            if provider_fields.get("data_limit_mb") is not None
            else None,
            "sms": _first_present(provider_fields, "sms_limit"),
            "daily": _normalize_usage_controls(
                _first_present(provider_fields, "consumption_daily")
            ),
            "monthly": _normalize_usage_controls(
                _first_present(provider_fields, "consumption_monthly")
            ),
        },
        "dates": {
            "activated_at": _iso_datetime(data.get("activated_at")),
            "updated_at": _iso_datetime(data.get("updated_at")),
            "added_at": _iso_datetime(_first_present(provider_fields, "date_added")),
            "provisioned_at": _iso_datetime(
                _first_present(provider_fields, "provision_date")
            ),
        },
        "custom_fields": _custom_fields(provider_fields),
    }


def _normalize_usage_controls(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    normalized: dict[str, Any] = {}
    for metric, payload in value.items():
        if not isinstance(payload, dict):
            normalized[metric] = payload
            continue
        normalized[metric] = {
            key: _boolish(payload.get(source_key))
            for key, source_key in (
                ("limit", "limit"),
                ("value", "value"),
                ("threshold_reached", "thr_reached"),
                ("traffic_cut", "traffic_cut"),
                ("enabled", "enabled"),
            )
        }
    return normalized


def _parse_metric_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    metrics = [item.strip() for item in raw.split(",") if item.strip()]
    return metrics or None


def _parse_custom_filters(raw: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in raw or []:
        separator = "=" if "=" in item else ":"
        if separator not in item:
            raise HTTPException(
                status_code=400,
                detail="custom filters must use key=value or key:value format",
            )
        key, value = item.split(separator, 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise HTTPException(
                status_code=400,
                detail="custom filters must include non-empty key and value",
            )
        parsed[key] = value
    return parsed


def _parse_query_dt(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    if not _REQUEST_DT_RE.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail=f"{field} must use yyyy-MM-ddTHH:mm:ssZ format",
        )
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _cursor_has_modified_since(cursor: str | None) -> bool:
    if not cursor:
        return False
    return any(part.startswith("since:") for part in cursor.split("|"))


def _tele2_missing_modified_since_response() -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "errorMessage": "ModifiedSince is required.",
            "errorCode": "10000003",
        },
    )


def _build_filters(
    *,
    status_filter: str | None,
    modified_since: str | None,
    modified_till: str | None,
    iccid: str | None,
    imsi: str | None,
    msisdn: str | None,
    custom: list[str] | None,
) -> SubscriptionSearchFilters:
    canonical_status = None
    if status_filter:
        try:
            canonical_status = AdministrativeStatus(status_filter)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown status '{status_filter}'. "
                    f"Valid values: {[s.value for s in AdministrativeStatus]}"
                ),
            )
    return SubscriptionSearchFilters(
        status=canonical_status,
        modified_since=_parse_query_dt(modified_since, "modified_since"),
        modified_till=_parse_query_dt(modified_till, "modified_till"),
        iccid=iccid,
        imsi=imsi,
        msisdn=msisdn,
        custom=_parse_custom_filters(custom),
    )


def _bootstrap_filters_for_provider(provider: str) -> SubscriptionSearchFilters:
    if provider == Provider.TELE2.value:
        return SubscriptionSearchFilters(
            modified_since=datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
        )
    return SubscriptionSearchFilters()


def _is_searchable_provider(adapter: Any) -> bool:
    return callable(getattr(adapter, "list_subscriptions", None))


def _adapter_bootstrap_filters(
    provider_name: str,
    adapter: Any,
) -> SubscriptionSearchFilters:
    bootstrap_filters = getattr(adapter, "bootstrap_filters", None)
    if callable(bootstrap_filters):
        return bootstrap_filters()
    return _bootstrap_filters_for_provider(provider_name)


def _adapter_supports_list_filter(
    provider_name: str,
    adapter: Any,
    filter_name: str,
) -> bool:
    supports_list_filter = getattr(adapter, "supports_list_filter", None)
    if callable(supports_list_filter):
        return bool(supports_list_filter(filter_name))
    if filter_name == "iccid":
        return provider_name in {Provider.KITE.value, Provider.TELE2.value}
    return False


# ── Routing map helpers ──────────────────────────────────────────────────────────


async def _upsert_routing(
    db: AsyncSession,
    iccid: str,
    provider: str,
    company_id: uuid.UUID,
) -> None:
    """Insert or update the routing index entry for this SIM."""
    stmt = (
        pg_insert(SimRoutingMap)
        .values(iccid=iccid, provider=provider, company_id=company_id)
        .on_conflict_do_update(
            index_elements=["iccid"],
            set_={
                "provider": provider,
                "company_id": company_id,
                "last_seen_at": func.now(),
            },
        )
    )
    await db.execute(stmt)


# ── Idempotency helpers ──────────────────────────────────────────────────────────


async def _claim_idempotency_key(
    key: str,
    company_id: uuid.UUID,
    db: AsyncSession,
) -> bool:
    """Atomically claim an idempotency key.

    Returns False when this company/key already exists and the provider call
    must not be repeated.
    """
    stmt = (
        pg_insert(IdempotencyKey)
        .values(
            key=key,
            response={"status": "processing"},
            company_id=company_id,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        .on_conflict_do_nothing(index_elements=["company_id", "key"])
        .returning(IdempotencyKey.id)
    )
    result = await db.execute(stmt)
    claimed = result.scalar_one_or_none() is not None
    await db.commit()
    return claimed


async def _mark_idempotency_processed(
    key: str,
    company_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    await db.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.key == key, IdempotencyKey.company_id == company_id)
        .values(response={"status": 204})
    )
    await db.commit()


async def _release_idempotency_key(
    key: str,
    company_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    await db.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.key == key,
            IdempotencyKey.company_id == company_id,
            IdempotencyKey.response == {"status": "processing"},
        )
    )
    await db.commit()


# ── Lifecycle audit helper ───────────────────────────────────────────────────────


async def _write_lifecycle_audit(
    db: AsyncSession,
    *,
    company_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    request_id: str | None,
    iccid: str,
    provider: str,
    action: str,
    target: str | None,
    idempotency_key: str | None,
    outcome: str,
    latency_ms: int | None,
    provider_request_id: str | None,
    provider_error_code: str | None,
    error: str | None,
) -> None:
    record = LifecycleChangeAudit(
        company_id=str(company_id),
        actor_id=str(actor_id) if actor_id else None,
        request_id=request_id,
        iccid=iccid,
        provider=provider,
        action=action,
        target=target,
        idempotency_key=idempotency_key,
        accepted_at=datetime.now(UTC) if outcome == "success" else None,
        outcome=outcome,
        latency_ms=latency_ms,
        provider_request_id=provider_request_id,
        provider_error_code=provider_error_code,
        error=error,
    )
    db.add(record)
    await db.commit()


def _provider_error_fields(exc: Exception) -> tuple[str | None, str | None]:
    if isinstance(exc, DomainError):
        return (
            exc.extra.get("provider_request_id") or exc.extra.get("transaction_id"),
            exc.extra.get("provider_error_code") or exc.extra.get("exception_id"),
        )
    return None, None


# ── Read endpoints ───────────────────────────────────────────────────────────────


async def _list_via_provider_search(
    provider: str,
    cursor: str | None,
    limit: int,
    filters: SubscriptionSearchFilters,
    company_id: uuid.UUID,
    db: AsyncSession,
    settings: Settings,
    registry: ProviderRegistry,
) -> SimListOut | JSONResponse:
    """Provider-scoped listing path.

    Delegates to the adapter's native `list_subscriptions`. Upserts each
    returned SIM into the routing index so subsequent single-SIM calls work.
    """
    creds = await _load_credentials(company_id, provider, db, settings)
    adapter = registry.get(provider)

    if not _is_searchable_provider(adapter):
        raise ListingPreconditionFailed(
            detail=(
                f"Provider '{provider}' does not expose a native subscription listing. "
                "Use the global listing endpoint (omit ?provider=) or fetch SIMs "
                "individually by ICCID."
            ),
            extra={"provider": provider, "missing_capability": "SearchableProvider"},
        )

    subs, next_cursor = await adapter.list_subscriptions(
        creds, cursor=cursor, limit=limit, filters=filters
    )

    # Lazy-populate the routing index with every SIM returned by the provider.
    for sub in subs:
        await _upsert_routing(db, sub.iccid, provider, company_id)
    if subs:
        await db.commit()

    return SimListOut(
        items=[_to_out(s) for s in subs],
        next_cursor=next_cursor,
        total=None,
        partial=False,
        failed_providers=[],
        provider_statuses=[
            ProviderStatusOut(provider=provider, status="ok", count=len(subs))
        ],
    )


def _encode_global_cursor(provider_cursors: dict[str, str | None]) -> str | None:
    active = {
        provider: cursor
        for provider, cursor in provider_cursors.items()
        if cursor is not None
    }
    if not active:
        return None
    payload = json.dumps(active, separators=(",", ":"), sort_keys=True).encode()
    token = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{_GLOBAL_CURSOR_PREFIX}{token}"


def _decode_global_cursor(cursor: str | None) -> dict[str, str | None] | None:
    if cursor is None:
        return None
    if not cursor.startswith(_GLOBAL_CURSOR_PREFIX):
        return {provider.value: cursor for provider in Provider}
    token = cursor[len(_GLOBAL_CURSOR_PREFIX) :]
    padded = token + ("=" * (-len(token) % 4))
    try:
        payload = base64.urlsafe_b64decode(padded.encode())
        decoded = json.loads(payload.decode())
    except ValueError, json.JSONDecodeError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {
        str(provider): str(provider_cursor)
        for provider, provider_cursor in decoded.items()
        if provider in {p.value for p in Provider} and provider_cursor is not None
    }


def _global_provider_failure(
    provider_name: str,
    exc: Exception,
) -> tuple[dict[str, str], ProviderStatusOut]:
    code = exc.code if isinstance(exc, DomainError) else "provider.unavailable"
    title = exc.title if isinstance(exc, DomainError) else "Provider request failed"
    return (
        {
            "provider": provider_name,
            "code": code,
            "title": title,
        },
        ProviderStatusOut(
            provider=provider_name,
            status="error",
            count=0,
            code=code,
            title=title,
        ),
    )


def _is_global_iccid_search(filters: SubscriptionSearchFilters) -> bool:
    return (
        bool(filters.iccid)
        and filters.status is None
        and filters.modified_since is None
        and filters.modified_till is None
        and not filters.imsi
        and not filters.msisdn
        and not filters.custom
    )


async def _list_global_iccid_search(
    filters: SubscriptionSearchFilters,
    company_id: uuid.UUID,
    db: AsyncSession,
    settings: Settings,
    registry: ProviderRegistry,
) -> SimListOut:
    iccid = filters.iccid or ""
    routing = await _find_routing(iccid, company_id, db)
    if routing is not None:
        creds = await _load_credentials(company_id, routing.provider, db, settings)
        adapter = registry.get(routing.provider)
        sub = await adapter.get_subscription(iccid, creds)
        return SimListOut(
            items=[_to_out(sub)],
            next_cursor=None,
            total=None,
            partial=False,
            failed_providers=[],
            provider_statuses=[
                ProviderStatusOut(
                    provider=provider.value,
                    status="ok"
                    if provider.value == routing.provider
                    else "not_queried",
                    count=1 if provider.value == routing.provider else 0,
                )
                for provider in Provider
            ],
        )

    items: list[SubscriptionOut] = []
    failed_providers_by_name: dict[str, dict[str, str]] = {}
    provider_statuses_by_name: dict[str, ProviderStatusOut] = {}
    provider_calls: list[tuple[str, Any, dict[str, Any]]] = []

    for provider in Provider:
        provider_name = provider.value
        try:
            if provider_name == Provider.MOABITS.value:
                unsupported = UnsupportedOperation(
                    detail="moabits list_subscriptions does not support ICCID filters"
                )
                failed_provider, provider_status = _global_provider_failure(
                    provider_name, unsupported
                )
                failed_providers_by_name[provider_name] = failed_provider
                provider_statuses_by_name[provider_name] = provider_status
                continue
            adapter = registry.get(provider_name)
            if not _is_searchable_provider(
                adapter
            ) or not _adapter_supports_list_filter(provider_name, adapter, "iccid"):
                unsupported = UnsupportedOperation(
                    detail=f"{provider_name} list_subscriptions does not support ICCID filters"
                )
                failed_provider, provider_status = _global_provider_failure(
                    provider_name, unsupported
                )
                failed_providers_by_name[provider_name] = failed_provider
                provider_statuses_by_name[provider_name] = provider_status
                continue
            creds = await _load_credentials(company_id, provider_name, db, settings)
            provider_calls.append((provider_name, adapter, creds))
        except Exception as exc:
            failed_provider, provider_status = _global_provider_failure(
                provider_name, exc
            )
            failed_providers_by_name[provider_name] = failed_provider
            provider_statuses_by_name[provider_name] = provider_status
            logger.warning(
                "global_iccid_search_provider_error",
                provider=provider_name,
                iccid=iccid,
                error=str(exc),
            )

    provider_results = await asyncio.gather(
        *(
            adapter.list_subscriptions(
                creds,
                cursor=None,
                limit=1,
                filters=dataclasses.replace(
                    _adapter_bootstrap_filters(provider_name, adapter),
                    iccid=iccid,
                ),
            )
            for provider_name, adapter, creds in provider_calls
        ),
        return_exceptions=True,
    )

    for (provider_name, _adapter, _creds), result in zip(
        provider_calls, provider_results, strict=True
    ):
        if isinstance(result, Exception):
            failed_provider, provider_status = _global_provider_failure(
                provider_name, result
            )
            failed_providers_by_name[provider_name] = failed_provider
            provider_statuses_by_name[provider_name] = provider_status
            logger.warning(
                "global_iccid_search_provider_error",
                provider=provider_name,
                iccid=iccid,
                error=str(result),
            )
            continue
        subs, _next_cursor = result
        for sub in subs:
            await _upsert_routing(db, sub.iccid, provider_name, company_id)
        items.extend(_to_out(sub) for sub in subs)
        provider_statuses_by_name[provider_name] = ProviderStatusOut(
            provider=provider_name,
            status="ok",
            count=len(subs),
        )

    if items:
        await db.commit()
    failed_providers = [
        failed_providers_by_name[provider.value]
        for provider in Provider
        if provider.value in failed_providers_by_name
    ]
    provider_statuses = [
        provider_statuses_by_name[provider.value]
        for provider in Provider
        if provider.value in provider_statuses_by_name
    ]
    return SimListOut(
        items=items,
        next_cursor=None,
        total=None,
        partial=bool(failed_providers),
        failed_providers=failed_providers,
        provider_statuses=provider_statuses,
    )


async def _list_via_routing_index(
    cursor: str | None,
    limit: int,
    filters: SubscriptionSearchFilters,
    company_id: uuid.UUID,
    db: AsyncSession,
    settings: Settings,
    registry: ProviderRegistry,
) -> SimListOut:
    """Global listing path — fans out to native provider listings."""
    if filters.has_filters:
        if _is_global_iccid_search(filters):
            return await _list_global_iccid_search(
                filters, company_id, db, settings, registry
            )
        raise UnsupportedOperation(
            detail="Canonical filters require provider-scoped listing (?provider=<name>)"
        )
    provider_cursors = _decode_global_cursor(cursor)
    items: list[SubscriptionOut] = []
    failed_providers_by_name: dict[str, dict[str, str]] = {}
    provider_statuses_by_name: dict[str, ProviderStatusOut] = {}
    next_provider_cursors: dict[str, str | None] = {}
    provider_calls: list[tuple[str, Any, dict[str, Any], str | None]] = []

    for provider in Provider:
        provider_name = provider.value
        if provider_cursors is not None and provider_name not in provider_cursors:
            provider_statuses_by_name[provider_name] = ProviderStatusOut(
                provider=provider_name, status="not_queried"
            )
            continue
        try:
            creds = await _load_credentials(company_id, provider_name, db, settings)
            adapter = registry.get(provider_name)
            if not _is_searchable_provider(adapter):
                provider_statuses_by_name[provider_name] = ProviderStatusOut(
                    provider=provider_name,
                    status="not_queried",
                    title="Provider does not expose native listing",
                )
                continue
            provider_calls.append(
                (
                    provider_name,
                    adapter,
                    creds,
                    None
                    if provider_cursors is None
                    else provider_cursors.get(provider_name),
                )
            )
        except Exception as exc:
            failed_provider, provider_status = _global_provider_failure(
                provider_name, exc
            )
            failed_providers_by_name[provider_name] = failed_provider
            provider_statuses_by_name[provider_name] = provider_status
            logger.warning(
                "global_listing_provider_error",
                provider=provider_name,
                error=str(exc),
            )

    provider_results = await asyncio.gather(
        *(
            adapter.list_subscriptions(
                creds,
                cursor=provider_cursor,
                limit=_GLOBAL_PROVIDER_PAGE_SIZE,
                filters=_adapter_bootstrap_filters(provider_name, adapter),
            )
            for provider_name, adapter, creds, provider_cursor in provider_calls
        ),
        return_exceptions=True,
    )

    for (provider_name, _adapter, _creds, _provider_cursor), result in zip(
        provider_calls, provider_results, strict=True
    ):
        if isinstance(result, Exception):
            failed_provider, provider_status = _global_provider_failure(
                provider_name, result
            )
            failed_providers_by_name[provider_name] = failed_provider
            provider_statuses_by_name[provider_name] = provider_status
            logger.warning(
                "global_listing_provider_error",
                provider=provider_name,
                error=str(result),
            )
            continue

        subs, next_cursor = result
        try:
            for sub in subs:
                await _upsert_routing(db, sub.iccid, provider_name, company_id)
            items.extend(_to_out(sub) for sub in subs)
            next_provider_cursors[provider_name] = next_cursor
            provider_statuses_by_name[provider_name] = ProviderStatusOut(
                provider=provider_name,
                status="ok",
                count=len(subs),
            )
        except Exception as exc:
            failed_provider, provider_status = _global_provider_failure(
                provider_name, exc
            )
            failed_providers_by_name[provider_name] = failed_provider
            provider_statuses_by_name[provider_name] = provider_status
            logger.warning(
                "global_listing_provider_error",
                provider=provider_name,
                error=str(exc),
            )

    failed_providers = [
        failed_providers_by_name[provider.value]
        for provider in Provider
        if provider.value in failed_providers_by_name
    ]
    provider_statuses = [
        provider_statuses_by_name[provider.value]
        for provider in Provider
        if provider.value in provider_statuses_by_name
    ]

    if items:
        await db.commit()
    if not items and failed_providers:
        raise ListingPreconditionFailed(
            detail=(
                "Provider bootstrap did not discover any SIMs. Pass "
                "?provider=<name> to inspect a specific source or import known "
                "ICCIDs via POST /v1/sims/import."
            ),
            extra={
                "reason": "routing_map_empty",
                "failed_providers": failed_providers,
            },
        )

    next_cursor = _encode_global_cursor(next_provider_cursors)
    return SimListOut(
        items=items,
        next_cursor=next_cursor,
        total=None,
        partial=bool(failed_providers),
        failed_providers=failed_providers,
        provider_statuses=provider_statuses,
    )


@router.get("", response_model=SimListOut)
async def list_sims(
    cursor: str | None = None,
    limit: int = 50,
    provider: Provider | None = Query(
        None,
        description="Provider-scoped listing adapter to use.",
    ),
    status_filter: str | None = Query(None, alias="status"),
    modified_since: str | None = Query(
        None,
        description=(
            "Normalized provider-scoped filter for SIMs changed since this UTC "
            "timestamp. Must include the trailing Z and use "
            "yyyy-MM-ddTHH:mm:ssZ. Provider support: tele2 (required, max "
            "1-year window), kite (optional, maps to startLastStateChangeDate), "
            "moabits (currently unsupported and ignored until an equivalent "
            "server-side filter is documented). Example: 2026-04-01T00:00:00Z."
        ),
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
        examples=["2026-04-01T00:00:00Z"],
    ),
    modified_till: str | None = Query(
        None,
        description=(
            "Normalized provider-scoped upper bound for SIM modification/state "
            "change filtering. Must include the trailing Z and use "
            "yyyy-MM-ddTHH:mm:ssZ. Provider support: tele2 (optional; defaults "
            "to modified_since + 1 year), kite (optional, maps to "
            "endLastStateChangeDate), moabits (currently unsupported and ignored "
            "until an equivalent server-side filter is documented)."
        ),
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
        examples=["2027-04-18T17:31:34Z"],
    ),
    iccid: str | None = None,
    imsi: str | None = None,
    msisdn: str | None = None,
    custom: list[str] | None = Query(None),
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> SimListOut | JSONResponse:
    """List SIMs for the caller's company.

    1. **Provider-scoped** (`?provider=kite|tele2|moabits`): delegates to the
       adapter's native listing. Also upserts returned SIMs into the routing
       index so subsequent single-SIM calls work without an explicit import.

    2. **Global** (no `provider`): fans out to searchable providers, requesting
       a small page from each one and upserting returned ICCIDs into the routing
       index for subsequent single-SIM calls.
    """
    if (
        provider == Provider.TELE2
        and modified_since is None
        and not _cursor_has_modified_since(cursor)
    ):
        return _tele2_missing_modified_since_response()
    filters = _build_filters(
        status_filter=status_filter,
        modified_since=modified_since,
        modified_till=modified_till,
        iccid=iccid,
        imsi=imsi,
        msisdn=msisdn,
        custom=custom,
    )
    if provider:
        return await _list_via_provider_search(
            provider.value,
            cursor,
            limit,
            filters,
            company_id,
            db,
            settings,
            registry,
        )
    return await _list_via_routing_index(
        cursor, limit, filters, company_id, db, settings, registry
    )


@router.get("/{iccid}", response_model=SubscriptionOut)
async def get_sim(
    iccid: str,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> SubscriptionOut:
    routing = await _resolve_routing(iccid, company_id, db)
    creds = await _load_credentials(company_id, routing.provider, db, settings)
    adapter = registry.get(routing.provider)
    sub = await adapter.get_subscription(iccid, creds)
    return _to_out(sub)


@router.get("/{iccid}/usage", response_model=UsageOut)
async def get_usage(
    iccid: str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    metrics: str | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> UsageOut:
    routing = await _resolve_routing(iccid, company_id, db)
    creds = await _load_credentials(company_id, routing.provider, db, settings)
    adapter = registry.get(routing.provider)
    snap = await adapter.get_usage(
        iccid,
        creds,
        start_date=start_date,
        end_date=end_date,
        metrics=_parse_metric_list(metrics),
    )
    return UsageOut(**dataclasses.asdict(snap))


@router.get("/{iccid}/presence", response_model=PresenceOut)
async def get_presence(
    iccid: str,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> PresenceOut:
    routing = await _resolve_routing(iccid, company_id, db)
    creds = await _load_credentials(company_id, routing.provider, db, settings)
    adapter = registry.get(routing.provider)
    presence = await adapter.get_presence(iccid, creds)
    return PresenceOut(**dataclasses.asdict(presence))


# ── Write endpoints ──────────────────────────────────────────────────────────────


@router.put("/{iccid}/status", status_code=status.HTTP_204_NO_CONTENT)
async def set_status(
    iccid: str,
    body: StatusChangeIn,
    request: Request,
    idempotency_key: str = Depends(_require_idempotency_key),
    current: Profile = Depends(require_roles(AppRole.admin)),
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> None:
    try:
        target = AdministrativeStatus(body.target)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown status '{body.target}'. Valid values: {[s.value for s in AdministrativeStatus]}",
        )

    routing = await _resolve_routing(iccid, company_id, db)
    if not await _claim_idempotency_key(idempotency_key, company_id, db):
        await _write_lifecycle_audit(
            db,
            company_id=company_id,
            actor_id=current.id,
            request_id=request.headers.get("X-Request-ID"),
            iccid=iccid,
            provider=routing.provider,
            action="set_status",
            target=body.target,
            idempotency_key=idempotency_key,
            outcome="replayed",
            latency_ms=0,
            provider_request_id=None,
            provider_error_code=None,
            error=None,
        )
        return

    creds = await _load_credentials(company_id, routing.provider, db, settings)
    adapter = registry.get(routing.provider)

    error: str | None = None
    outcome = "success"
    provider_request_id = None
    provider_error_code = None
    started = time.perf_counter()
    try:
        await adapter.set_administrative_status(
            iccid,
            creds,
            target=target,
            idempotency_key=idempotency_key,
            data_service=body.data_service,
            sms_service=body.sms_service,
        )
    except Exception as exc:
        error = str(exc)
        outcome = "error"
        provider_request_id, provider_error_code = _provider_error_fields(exc)
        await _release_idempotency_key(idempotency_key, company_id, db)
        raise
    finally:
        await _write_lifecycle_audit(
            db,
            company_id=company_id,
            actor_id=current.id,
            request_id=request.headers.get("X-Request-ID"),
            iccid=iccid,
            provider=routing.provider,
            action="set_status",
            target=body.target,
            idempotency_key=idempotency_key,
            outcome=outcome,
            latency_ms=int((time.perf_counter() - started) * 1000),
            provider_request_id=provider_request_id,
            provider_error_code=provider_error_code,
            error=error,
        )

    await _mark_idempotency_processed(idempotency_key, company_id, db)


@router.post("/{iccid}/purge", status_code=status.HTTP_204_NO_CONTENT)
async def purge_sim(
    iccid: str,
    request: Request,
    idempotency_key: str = Depends(_require_idempotency_key),
    current: Profile = Depends(require_roles(AppRole.admin)),
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> None:
    routing = await _resolve_routing(iccid, company_id, db)
    if not await _claim_idempotency_key(idempotency_key, company_id, db):
        await _write_lifecycle_audit(
            db,
            company_id=company_id,
            actor_id=current.id,
            request_id=request.headers.get("X-Request-ID"),
            iccid=iccid,
            provider=routing.provider,
            action="purge",
            target=None,
            idempotency_key=idempotency_key,
            outcome="replayed",
            latency_ms=0,
            provider_request_id=None,
            provider_error_code=None,
            error=None,
        )
        return

    creds = await _load_credentials(company_id, routing.provider, db, settings)
    adapter = registry.get(routing.provider)

    error: str | None = None
    outcome = "success"
    provider_request_id = None
    provider_error_code = None
    started = time.perf_counter()
    try:
        await adapter.purge(iccid, creds, idempotency_key=idempotency_key)
    except Exception as exc:
        error = str(exc)
        outcome = "error"
        provider_request_id, provider_error_code = _provider_error_fields(exc)
        await _release_idempotency_key(idempotency_key, company_id, db)
        raise
    finally:
        await _write_lifecycle_audit(
            db,
            company_id=company_id,
            actor_id=current.id,
            request_id=request.headers.get("X-Request-ID"),
            iccid=iccid,
            provider=routing.provider,
            action="purge",
            target=None,
            idempotency_key=idempotency_key,
            outcome=outcome,
            latency_ms=int((time.perf_counter() - started) * 1000),
            provider_request_id=provider_request_id,
            provider_error_code=provider_error_code,
            error=error,
        )

    await _mark_idempotency_processed(idempotency_key, company_id, db)


# ── Import endpoint ──────────────────────────────────────────────────────────────


@router.post("/import", response_model=SimImportOut, status_code=status.HTTP_200_OK)
async def import_sims(
    body: SimImportIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    registry: ProviderRegistry = Depends(get_registry),
) -> SimImportOut:
    """Bootstrap the routing index for a set of SIMs.

    Accepts a list of `{iccid, provider}` pairs and upserts them into the
    `sim_routing_map` table. Use this when SIM ICCIDs are known in advance
    (e.g. from a CSV export) and you want single-SIM endpoints to work before
    running a full provider listing.

    The provider must be registered (kite, tele2, moabits). Each entry is
    idempotent — importing the same ICCID twice updates `last_seen_at` only.
    """
    known = set(registry.registered_providers())
    unknown = {item.provider for item in body.sims if item.provider not in known}
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown provider(s): {sorted(unknown)}. Valid: {sorted(known)}",
        )

    for item in body.sims:
        await _upsert_routing(db, item.iccid, item.provider, company_id)
    if body.sims:
        await db.commit()

    return SimImportOut(imported=len(body.sims))
