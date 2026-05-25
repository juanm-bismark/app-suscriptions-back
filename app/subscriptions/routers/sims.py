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
from typing import Annotated, Any, Literal

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
    BatchTooLarge,
    CredentialsMissing,
    DomainError,
    IdempotencyKeyRequired,
    ListingPreconditionFailed,
    ProviderRateLimited,
    ProviderResourceNotFound,
    ProviderUnavailable,
    SubscriptionNotFound,
    UnsupportedOperation,
)
from app.subscriptions.domain import (
    Subscription,
    SubscriptionSearchFilters,
)
from app.subscriptions.models.lifecycle_audit import LifecycleChangeAudit
from app.subscriptions.models.routing import SimRoutingMap, SimRoutingPrefixMap
from app.subscriptions.schemas.sim import (
    PresenceOut,
    ProviderStatusOut,
    SimDetailsErrorOut,
    SimDetailsIn,
    SimDetailsItemOut,
    SimDetailsOut,
    SimDetailsSummaryOut,
    SimImportIn,
    SimImportOut,
    SimListOut,
    SimSearchIn,
    SimSearchProviderFilters,
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
_ICCID_DIGITS_RE = re.compile(r"\D+")
_ICCID_ROUTING_PREFIX_LENGTH = 6
_GLOBAL_CURSOR_PREFIX = "global:"
_STATUS_CURSOR_PREFIX = "statuses:"


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


def _normalize_iccid_for_routing(iccid: str) -> str:
    return _ICCID_DIGITS_RE.sub("", iccid)


def _routing_iccid(routing: Any, requested_iccid: str) -> str:
    routed = getattr(routing, "iccid", None)
    if routed:
        return str(routed)
    return _normalize_iccid_for_routing(requested_iccid) or requested_iccid.strip()


async def _find_routing(
    iccid: str,
    company_id: uuid.UUID,
    db: AsyncSession,
) -> SimRoutingMap | None:
    route_iccid = _normalize_iccid_for_routing(iccid) or iccid.strip()
    result = await db.execute(
        select(SimRoutingMap).where(
            SimRoutingMap.iccid == route_iccid,
            SimRoutingMap.company_id == company_id,
        )
    )
    return result.scalar_one_or_none()


def _iccid_routing_prefix(iccid: str) -> str | None:
    digits = _normalize_iccid_for_routing(iccid)
    if len(digits) < _ICCID_ROUTING_PREFIX_LENGTH:
        return None
    return digits[:_ICCID_ROUTING_PREFIX_LENGTH]


async def _find_prefix_routing(
    iccid: str,
    company_id: uuid.UUID,
    db: AsyncSession,
) -> SimRoutingMap | None:
    iccid_prefix = _iccid_routing_prefix(iccid)
    if iccid_prefix is None:
        return None

    result = await db.execute(
        select(SimRoutingPrefixMap).where(
            SimRoutingPrefixMap.iccid_prefix == iccid_prefix,
        )
    )
    prefix = result.scalar_one_or_none()
    if prefix is None:
        return None

    route_iccid = _normalize_iccid_for_routing(iccid) or iccid.strip()
    return SimRoutingMap(
        iccid=route_iccid,
        provider=prefix.provider,
        company_id=company_id,
    )


# ── Lazy ICCID resolution (routing map → cross-provider fan-out) ────────────────


_NEGATIVE_CACHE_TTL_SECONDS = 60.0
_NEGATIVE_CACHE_MAX_ENTRIES = 10_000
# Process-local cache; per worker is fine — a stale miss just costs one extra
# fan-out, never a wrong answer.
_iccid_negative_cache: dict[tuple[uuid.UUID, str], float] = {}


def _negative_cache_hit(company_id: uuid.UUID, iccid: str) -> bool:
    key = (company_id, iccid)
    expires_at = _iccid_negative_cache.get(key)
    if expires_at is None:
        return False
    if time.monotonic() >= expires_at:
        _iccid_negative_cache.pop(key, None)
        return False
    return True


def _negative_cache_record(company_id: uuid.UUID, iccid: str) -> None:
    now = time.monotonic()
    if len(_iccid_negative_cache) >= _NEGATIVE_CACHE_MAX_ENTRIES:
        for key, expires_at in list(_iccid_negative_cache.items()):
            if expires_at <= now:
                _iccid_negative_cache.pop(key, None)
        if len(_iccid_negative_cache) >= _NEGATIVE_CACHE_MAX_ENTRIES:
            _iccid_negative_cache.pop(next(iter(_iccid_negative_cache)), None)
    _iccid_negative_cache[(company_id, iccid)] = now + _NEGATIVE_CACHE_TTL_SECONDS


def _negative_cache_forget(company_id: uuid.UUID, iccid: str) -> None:
    _iccid_negative_cache.pop((company_id, iccid), None)


def _unresolved_iccid_error(iccid: str) -> SubscriptionNotFound:
    return SubscriptionNotFound(
        detail=(
            f"SIM {iccid} not found in any registered provider that supports "
            "ICCID lookup. Verify the ICCID is correct. If the SIM lives on a "
            "provider whose listing API cannot filter by ICCID (e.g. Moabits "
            "without a populated company code), bootstrap the routing index via "
            "POST /v1/sims/import or a provider-scoped listing."
        )
    )


async def _discover_iccid_across_providers(
    iccid: str,
    company_id: uuid.UUID,
    db: AsyncSession,
    settings: Settings,
    registry: ProviderRegistry,
) -> Subscription | None:
    """Fan out to every provider that supports listing filtered by ICCID.

    Upserts the routing map for every SIM the providers return and commits
    once at the end. Returns the Subscription whose ICCID matches the query
    (so callers can short-circuit a second provider call), or None when no
    provider claimed it. Provider-level failures during setup or the search
    itself are logged and treated as misses for that provider — discovery
    succeeds if *any* provider returns a match.
    """
    provider_calls: list[tuple[str, Any, dict[str, Any]]] = []
    for provider in Provider:
        provider_name = provider.value
        try:
            adapter = registry.get(provider_name)
            if not _is_searchable_provider(adapter):
                continue
            if not _adapter_supports_list_filter(provider_name, adapter, "iccid"):
                continue
            creds = await _load_credentials(company_id, provider_name, db, settings)
        except Exception as exc:
            logger.warning(
                "iccid_discovery_setup_error",
                provider=provider_name,
                iccid=iccid,
                error=str(exc),
            )
            continue
        provider_calls.append((provider_name, adapter, creds))

    if not provider_calls:
        return None

    results = await asyncio.gather(
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

    matched: Subscription | None = None
    any_upserted = False
    for (provider_name, _adapter, _creds), result in zip(
        provider_calls, results, strict=True
    ):
        if isinstance(result, Exception):
            logger.warning(
                "iccid_discovery_provider_error",
                provider=provider_name,
                iccid=iccid,
                error=str(result),
            )
            continue
        subs, _next_cursor = result
        for sub in subs:
            await _upsert_routing(db, sub.iccid, provider_name, company_id)
            any_upserted = True
            if matched is None and sub.iccid == iccid:
                matched = sub
    if any_upserted:
        await db.commit()
    return matched


async def _resolve_routing_or_discover(
    iccid: str,
    company_id: uuid.UUID,
    db: AsyncSession,
    settings: Settings,
    registry: ProviderRegistry,
) -> tuple[SimRoutingMap, Subscription | None]:
    """Resolve normalized ICCID → exact route → prefix route → discovery.

    Returns (routing_entry, prefetched_subscription_or_None). The prefetched
    Subscription is populated only when discovery hit a provider, letting the
    caller skip a second provider round-trip. Raises SubscriptionNotFound when
    neither the routing map nor any provider claims the ICCID.
    """
    route_iccid = _normalize_iccid_for_routing(iccid) or iccid.strip()
    routing = await _find_routing(route_iccid, company_id, db)
    if routing is not None:
        return routing, None

    routing = await _find_prefix_routing(route_iccid, company_id, db)
    if routing is not None:
        return routing, None

    if _negative_cache_hit(company_id, route_iccid):
        raise _unresolved_iccid_error(route_iccid)

    discovered = await _discover_iccid_across_providers(
        route_iccid, company_id, db, settings, registry
    )
    if discovered is None:
        _negative_cache_record(company_id, route_iccid)
        raise _unresolved_iccid_error(route_iccid)

    _negative_cache_forget(company_id, route_iccid)
    routing = await _find_routing(route_iccid, company_id, db)
    if routing is None:
        raise SubscriptionNotFound(
            detail=(
                f"SIM {iccid} was discovered on provider '{discovered.provider}' "
                "but the routing entry could not be persisted. Retry the request."
            )
        )
    return routing, discovered


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


_STATUS_GROUP_BY_VALUE = {
    "ACTIVE": "active_like",
    "ACTIVATED": "active_like",
    "TEST": "test_like",
    "READY": "test_like",
    "TEST_READY": "test_like",
    "SUSPENDED": "suspended_like",
    "INACTIVE_NEW": "inactive_like",
    "ACTIVATION_READY": "activation_ready_like",
    "ACTIVATION_PENDANT": "activation_pending_like",
    "PENDING": "activation_pending_like",
    "DEACTIVATED": "terminal_like",
    "PURGED": "purged_like",
    "INVENTORY": "inventory_like",
    "REPLACED": "replaced_like",
    "RETIRED": "terminal_like",
    "RESTORE": "restore_like",
    "UNKNOWN": "unknown",
}


def _status_label(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    return cleaned.replace("_", " ").title()


def _status_group(value: Any) -> str:
    if value is None:
        return "unknown"
    normalized = str(value).strip().replace(" ", "_").replace("-", "_").upper()
    if not normalized:
        return "unknown"
    return _STATUS_GROUP_BY_VALUE.get(normalized, "other")


def _status_group_label(group: str) -> str:
    if group == "unknown":
        return "Unknown"
    if group == "other":
        return "Other"
    if group.endswith("_like"):
        return f"{group[:-5].replace('_', ' ').title()}-like"
    return group.replace("_", " ").title()


def _normalized_subscription(data: dict[str, Any]) -> dict[str, Any]:
    provider_fields: dict[str, Any] = data.get("provider_fields") or {}
    services = provider_fields.get("services")
    if services is None and provider_fields.get("services_raw"):
        services = [
            item.strip().lower()
            for item in str(provider_fields["services_raw"]).split("/")
            if item.strip()
        ]
    status_group = _status_group(data.get("status"))

    return {
        "identity": {
            "imei": _first_present(provider_fields, "imei"),
            "alias": _first_present(provider_fields, "alias"),
            "eid": _first_present(provider_fields, "eid"),
            "euiccid": _first_present(provider_fields, "euiccid"),
            "sim_profile_id": _first_present(provider_fields, "sim_profile_id"),
        },
        "status": {
            "label": _status_label(data.get("status")),
            "group": status_group,
            "group_label": _status_group_label(status_group),
            "source": "provider",
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
            "ip_address": _first_present(provider_fields, "ip_address"),
            "ipv6_address": _first_present(provider_fields, "ipv6_address"),
            "fixed_ip_address": _first_present(
                provider_fields, "fixed_ip_address", "static_ip"
            ),
            "fixed_ipv6_address": _first_present(provider_fields, "fixed_ipv6_address"),
            "static_ips": _first_present(provider_fields, "static_ips"),
            "additional_static_ips": _first_present(
                provider_fields, "additional_static_ips"
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
            "gprs_status": _first_present(provider_fields, "gprs_status"),
            "ip_status": _first_present(provider_fields, "ip_status"),
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
    return SubscriptionSearchFilters(
        status=status_filter or None,
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
        return provider_name in {
            Provider.KITE.value,
            Provider.TELE2.value,
            Provider.MOABITS.value,
        }
    return False


# ── Routing map helpers ──────────────────────────────────────────────────────────


async def _upsert_routing(
    db: AsyncSession,
    iccid: str,
    provider: str,
    company_id: uuid.UUID,
) -> None:
    """Insert or update the routing index entry for this SIM."""
    route_iccid = _normalize_iccid_for_routing(iccid) or iccid.strip()
    stmt = (
        pg_insert(SimRoutingMap)
        .values(iccid=route_iccid, provider=provider, company_id=company_id)
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
    if not provider_cursors:
        return None
    payload = json.dumps(
        provider_cursors,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
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
    except (ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {
        str(provider): str(provider_cursor) if provider_cursor is not None else None
        for provider, provider_cursor in decoded.items()
        if provider in {p.value for p in Provider}
    }


def _status_cursor_key(status_value: str | None) -> str:
    return status_value or ""


def _encode_status_cursor(status_cursors: dict[str, str | None]) -> str | None:
    active_cursors = {
        status: cursor for status, cursor in status_cursors.items() if cursor is not None
    }
    if not active_cursors:
        return None
    payload = json.dumps(
        active_cursors,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    token = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{_STATUS_CURSOR_PREFIX}{token}"


def _decode_status_cursor(cursor: str | None) -> dict[str, str | None] | None:
    if cursor is None or not cursor.startswith(_STATUS_CURSOR_PREFIX):
        return None
    token = cursor[len(_STATUS_CURSOR_PREFIX) :]
    padded = token + ("=" * (-len(token) % 4))
    try:
        payload = base64.urlsafe_b64decode(padded.encode())
        decoded = json.loads(payload.decode())
    except (ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {
        str(status): str(status_cursor) if status_cursor is not None else None
        for status, status_cursor in decoded.items()
    }


def _global_provider_call_limits(page_limit: int, provider_count: int) -> list[int]:
    if provider_count <= 0:
        return []
    base, remainder = divmod(page_limit, provider_count)
    return [base + (1 if index < remainder else 0) for index in range(provider_count)]


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
    requested_iccid = filters.iccid or ""
    iccid = _normalize_iccid_for_routing(requested_iccid) or requested_iccid.strip()
    routing = await _find_routing(iccid, company_id, db)
    if routing is None:
        routing = await _find_prefix_routing(iccid, company_id, db)
    if routing is not None:
        creds = await _load_credentials(company_id, routing.provider, db, settings)
        adapter = registry.get(routing.provider)
        routed_iccid = _routing_iccid(routing, iccid)
        sub = await adapter.get_subscription(routed_iccid, creds)
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
    page_limit = min(max(limit, 1), 500)
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
        provider_cursor = (
            None if provider_cursors is None else provider_cursors.get(provider_name)
        )
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
                    provider_cursor,
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

    active_provider_calls: list[tuple[str, Any, dict[str, Any], str | None, int]] = []
    for (
        provider_name,
        adapter,
        creds,
        provider_cursor,
    ), call_limit in zip(
        provider_calls,
        _global_provider_call_limits(page_limit, len(provider_calls)),
        strict=True,
    ):
        if call_limit <= 0:
            next_provider_cursors[provider_name] = provider_cursor
            provider_statuses_by_name[provider_name] = ProviderStatusOut(
                provider=provider_name,
                status="not_queried",
                count=0,
            )
            continue
        active_provider_calls.append(
            (provider_name, adapter, creds, provider_cursor, call_limit)
        )

    provider_results = await asyncio.gather(
        *(
            adapter.list_subscriptions(
                creds,
                cursor=provider_cursor,
                limit=call_limit,
                filters=_adapter_bootstrap_filters(provider_name, adapter),
            )
            for (
                provider_name,
                adapter,
                creds,
                provider_cursor,
                call_limit,
            ) in active_provider_calls
        ),
        return_exceptions=True,
    )

    for (provider_name, _adapter, _creds, _provider_cursor, _call_limit), result in zip(
        active_provider_calls, provider_results, strict=True
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
            if next_cursor is not None:
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


def _merge_search_filters(
    body: SimSearchIn,
    provider_filters: SimSearchProviderFilters,
    status: str | None,
) -> SubscriptionSearchFilters:
    common = body.common
    return SubscriptionSearchFilters(
        status=status,
        modified_since=provider_filters.modified_since or common.modified_since,
        modified_till=provider_filters.modified_till or common.modified_till,
        iccid=provider_filters.iccid or common.iccid,
        imsi=provider_filters.imsi or common.imsi,
        msisdn=provider_filters.msisdn or common.msisdn,
        custom={**common.custom, **provider_filters.custom},
    )


def _search_status_values(
    provider_filters: SimSearchProviderFilters,
) -> list[str | None]:
    statuses: list[str | None] = []
    if provider_filters.status:
        statuses.append(provider_filters.status)
    statuses.extend(status for status in provider_filters.statuses if status)
    deduped = list(dict.fromkeys(statuses))
    return deduped or [None]


async def _search_via_provider_filters(
    body: SimSearchIn,
    company_id: uuid.UUID,
    db: AsyncSession,
    settings: Settings,
    registry: ProviderRegistry,
) -> SimListOut:
    """Fan out with provider-specific filters and merge the results."""
    selected_providers = body.providers or {
        provider: SimSearchProviderFilters() for provider in Provider
    }
    cursor_by_provider = _decode_global_cursor(body.cursor)
    provider_statuses_by_name: dict[str, ProviderStatusOut] = {
        provider.value: ProviderStatusOut(
            provider=provider.value,
            status="not_queried",
        )
        for provider in Provider
        if provider not in selected_providers
    }
    failed_providers_by_name: dict[str, dict[str, str]] = {}
    next_provider_cursors: dict[str, str | None] = {}
    provider_calls: list[
        tuple[
            str,
            Any,
            dict[str, Any],
            str | None,
            int,
            bool,
            str,
            SubscriptionSearchFilters,
        ]
    ] = []
    next_status_cursors_by_provider: dict[str, dict[str, str | None]] = {}

    selected_items = list(selected_providers.items())
    default_limits = _global_provider_call_limits(
        body.limit,
        sum(1 for _provider, filters in selected_items if filters.limit is None),
    )
    default_limit_iter = iter(default_limits)

    for provider, provider_filters in selected_items:
        provider_name = provider.value
        if cursor_by_provider is not None and provider_name not in cursor_by_provider:
            provider_statuses_by_name[provider_name] = ProviderStatusOut(
                provider=provider_name, status="not_queried"
            )
            continue
        provider_cursor = provider_filters.cursor
        if provider_cursor is None and cursor_by_provider is not None:
            provider_cursor = cursor_by_provider.get(provider_name)
        call_limit = (
            provider_filters.limit
            if provider_filters.limit is not None
            else next(default_limit_iter, body.limit)
        )
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
            requested_status_values = _search_status_values(provider_filters)
            multi_status_request = len(requested_status_values) > 1
            status_values = requested_status_values
            status_cursor_by_value = _decode_status_cursor(provider_cursor)
            if status_cursor_by_value is not None:
                status_values = [
                    status_value
                    for status_value in status_values
                    if _status_cursor_key(status_value) in status_cursor_by_value
                ]
                if not status_values:
                    provider_statuses_by_name[provider_name] = ProviderStatusOut(
                        provider=provider_name,
                        status="not_queried",
                    )
                    continue
            status_limits = _global_provider_call_limits(call_limit, len(status_values))
            for status_value, status_limit in zip(
                status_values,
                status_limits,
                strict=True,
            ):
                status_key = _status_cursor_key(status_value)
                status_cursor = (
                    status_cursor_by_value.get(status_key)
                    if status_cursor_by_value is not None
                    else provider_cursor
                )
                provider_calls.append(
                    (
                        provider_name,
                        adapter,
                        creds,
                        status_cursor,
                        status_limit,
                        not multi_status_request,
                        status_key,
                        _merge_search_filters(body, provider_filters, status_value),
                    )
                )
        except Exception as exc:
            failed_provider, provider_status = _global_provider_failure(
                provider_name, exc
            )
            failed_providers_by_name[provider_name] = failed_provider
            provider_statuses_by_name[provider_name] = provider_status
            logger.warning(
                "provider_search_setup_error",
                provider=provider_name,
                error=str(exc),
            )

    provider_results = await asyncio.gather(
        *(
            adapter.list_subscriptions(
                creds,
                cursor=provider_cursor,
                limit=call_limit,
                filters=filters,
            )
            for (
                _provider_name,
                adapter,
                creds,
                provider_cursor,
                call_limit,
                _use_next_cursor,
                _status_key,
                filters,
            ) in provider_calls
        ),
        return_exceptions=True,
    )

    items: list[SubscriptionOut] = []
    seen_items: set[tuple[str, str]] = set()
    provider_success_counts: dict[str, int] = {}
    provider_errors_by_name: dict[str, tuple[dict[str, str], ProviderStatusOut]] = {}
    for (
        provider_name,
        _adapter,
        _creds,
        _provider_cursor,
        _call_limit,
        use_next_cursor,
        status_key,
        _filters,
    ), result in zip(provider_calls, provider_results, strict=True):
        if isinstance(result, Exception):
            failed_provider, provider_status = _global_provider_failure(
                provider_name, result
            )
            provider_errors_by_name[provider_name] = (
                failed_provider,
                provider_status,
            )
            logger.warning(
                "provider_search_error",
                provider=provider_name,
                error=str(result),
            )
            continue

        subs, next_cursor = result
        for sub in subs:
            await _upsert_routing(db, sub.iccid, provider_name, company_id)
            item_key = (provider_name, sub.iccid)
            if item_key in seen_items:
                continue
            seen_items.add(item_key)
            items.append(_to_out(sub))
            provider_success_counts[provider_name] = (
                provider_success_counts.get(provider_name, 0) + 1
            )
        if use_next_cursor and next_cursor is not None:
            next_provider_cursors[provider_name] = next_cursor
        elif not use_next_cursor and next_cursor is not None:
            next_status_cursors_by_provider.setdefault(provider_name, {})[
                status_key
            ] = next_cursor

    for provider_name, status_cursors in next_status_cursors_by_provider.items():
        encoded_status_cursor = _encode_status_cursor(status_cursors)
        if encoded_status_cursor is not None:
            next_provider_cursors[provider_name] = encoded_status_cursor

    for provider, _provider_filters in selected_items:
        provider_name = provider.value
        if provider_name in provider_statuses_by_name:
            continue
        success_count = provider_success_counts.get(provider_name, 0)
        if provider_name in provider_errors_by_name:
            failed_provider, provider_status = provider_errors_by_name[provider_name]
            failed_providers_by_name[provider_name] = failed_provider
            provider_statuses_by_name[provider_name] = ProviderStatusOut(
                provider=provider_name,
                status="partial" if success_count else "error",
                count=success_count,
                code=provider_status.code,
                title=provider_status.title,
            )
            continue
        provider_statuses_by_name[provider_name] = ProviderStatusOut(
            provider=provider_name,
            status="ok",
            count=success_count,
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
        next_cursor=_encode_global_cursor(next_provider_cursors),
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


@router.post("/search", response_model=SimListOut)
async def search_sims(
    body: SimSearchIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> SimListOut:
    """Search SIMs with provider-specific filters and a merged table response."""
    return await _search_via_provider_filters(
        body,
        company_id,
        db,
        settings,
        registry,
    )


def _details_error_from_exception(exc: Exception) -> tuple[
    Literal["not_found", "timeout", "error", "rate_limited"],
    SimDetailsErrorOut,
]:
    if isinstance(exc, (SubscriptionNotFound, ProviderResourceNotFound)):
        return (
            "not_found",
            SimDetailsErrorOut(
                code=exc.code if isinstance(exc, DomainError) else "subscription.not_found",
                detail=exc.detail or str(exc),
            ),
        )
    if isinstance(exc, ProviderRateLimited):
        return (
            "rate_limited",
            SimDetailsErrorOut(
                code=exc.code,
                detail=exc.detail,
                retry_after=exc.extra.get("retry_after"),
            ),
        )
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return (
            "timeout",
            SimDetailsErrorOut(
                code="provider.unavailable",
                detail="Provider request timed out",
            ),
        )
    if isinstance(exc, ProviderUnavailable) and "timeout" in (
        (exc.detail or exc.title).lower()
    ):
        return (
            "timeout",
            SimDetailsErrorOut(code=exc.code, detail=exc.detail),
        )
    if isinstance(exc, DomainError):
        return (
            "error",
            SimDetailsErrorOut(
                code=exc.code,
                detail=exc.detail,
                retry_after=exc.extra.get("retry_after"),
            ),
        )
    return (
        "error",
        SimDetailsErrorOut(code="provider.unavailable", detail=str(exc)),
    )


@router.post("/details", response_model=SimDetailsOut)
async def get_sim_details_batch(
    body: SimDetailsIn,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> SimDetailsOut:
    """Resolve mixed-provider ICCIDs and fetch live details per SIM."""
    max_batch = settings.max_batch_details
    if not body.iccids:
        raise HTTPException(status_code=400, detail="iccids must contain at least one item")
    if len(body.iccids) > max_batch:
        raise BatchTooLarge(
            detail=f"Batch contains {len(body.iccids)} ICCIDs; max is {max_batch}",
            extra={"max_batch_details": max_batch},
        )

    requested_iccids = list(dict.fromkeys(body.iccids))
    allowed_providers = {provider.value for provider in body.providers or []}
    unresolved: list[str] = []
    filtered_out: list[str] = []
    resolved: list[tuple[str, str, str]] = []

    for requested_iccid in requested_iccids:
        try:
            routing, prefetched = await _resolve_routing_or_discover(
                requested_iccid, company_id, db, settings, registry
            )
        except SubscriptionNotFound:
            unresolved.append(requested_iccid)
            continue

        if allowed_providers and routing.provider not in allowed_providers:
            filtered_out.append(requested_iccid)
            continue

        if prefetched is not None:
            resolved.append((requested_iccid, prefetched.provider, prefetched.iccid))
            continue
        resolved.append(
            (requested_iccid, routing.provider, _routing_iccid(routing, requested_iccid))
        )

    credentials_by_provider: dict[str, dict[str, Any]] = {}
    adapter_by_provider: dict[str, Any] = {}
    provider_setup_errors: dict[str, Exception] = {}
    for _requested_iccid, provider, _routed_iccid in resolved:
        if provider in credentials_by_provider or provider in provider_setup_errors:
            continue
        try:
            credentials_by_provider[provider] = await _load_credentials(
                company_id, provider, db, settings
            )
            adapter_by_provider[provider] = registry.get(provider)
        except Exception as exc:
            provider_setup_errors[provider] = exc

    async def _fetch_one(
        requested_iccid: str, provider: str, routed_iccid: str
    ) -> tuple[str, SimDetailsItemOut]:
        setup_error = provider_setup_errors.get(provider)
        if setup_error is not None:
            status_value, error = _details_error_from_exception(setup_error)
            return requested_iccid, SimDetailsItemOut(
                provider=provider, status=status_value, error=error
            )
        try:
            sub = await adapter_by_provider[provider].get_subscription(
                routed_iccid, credentials_by_provider[provider]
            )
            return requested_iccid, SimDetailsItemOut(
                provider=provider, status="ok", data=_to_out(sub)
            )
        except Exception as exc:
            status_value, error = _details_error_from_exception(exc)
            return requested_iccid, SimDetailsItemOut(
                provider=provider, status=status_value, error=error
            )

    fetched = await asyncio.gather(*(_fetch_one(*item) for item in resolved))
    results = {iccid: item for iccid, item in fetched}

    summary = SimDetailsSummaryOut(total=len(results))
    for item in results.values():
        setattr(summary, item.status, getattr(summary, item.status) + 1)

    return SimDetailsOut(
        results=results,
        summary=summary,
        unresolved=unresolved,
        filtered_out=filtered_out,
    )


@router.get("/{iccid}", response_model=SubscriptionOut)
async def get_sim(
    iccid: str,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> SubscriptionOut:
    routing, prefetched = await _resolve_routing_or_discover(
        iccid, company_id, db, settings, registry
    )
    if prefetched is not None:
        return _to_out(prefetched)
    creds = await _load_credentials(company_id, routing.provider, db, settings)
    adapter = registry.get(routing.provider)
    routed_iccid = _routing_iccid(routing, iccid)
    sub = await adapter.get_subscription(routed_iccid, creds)
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
    routing, _ = await _resolve_routing_or_discover(
        iccid, company_id, db, settings, registry
    )
    creds = await _load_credentials(company_id, routing.provider, db, settings)
    adapter = registry.get(routing.provider)
    routed_iccid = _routing_iccid(routing, iccid)
    snap = await adapter.get_usage(
        routed_iccid,
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
    routing, _ = await _resolve_routing_or_discover(
        iccid, company_id, db, settings, registry
    )
    creds = await _load_credentials(company_id, routing.provider, db, settings)
    adapter = registry.get(routing.provider)
    routed_iccid = _routing_iccid(routing, iccid)
    presence = await adapter.get_presence(routed_iccid, creds)
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
    routing = await _resolve_routing(iccid, company_id, db)
    routed_iccid = _routing_iccid(routing, iccid)
    if not await _claim_idempotency_key(idempotency_key, company_id, db):
        await _write_lifecycle_audit(
            db,
            company_id=company_id,
            actor_id=current.id,
            request_id=request.headers.get("X-Request-ID"),
            iccid=routed_iccid,
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
            routed_iccid,
            creds,
            target=body.target,
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
            iccid=routed_iccid,
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
    routed_iccid = _routing_iccid(routing, iccid)
    if not await _claim_idempotency_key(idempotency_key, company_id, db):
        await _write_lifecycle_audit(
            db,
            company_id=company_id,
            actor_id=current.id,
            request_id=request.headers.get("X-Request-ID"),
            iccid=routed_iccid,
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
        await adapter.purge(routed_iccid, creds, idempotency_key=idempotency_key)
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
            iccid=routed_iccid,
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
