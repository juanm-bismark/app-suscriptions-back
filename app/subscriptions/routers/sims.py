"""SIM management API endpoints.

All endpoints are scoped to the caller's company via their JWT profile.
The routing table (SimRoutingMap) maps iccid → provider + company_id and is
populated lazily on first successful list, or explicitly via POST /import.
"""

import asyncio
import base64
import dataclasses
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import get_db
from app.identity.dependencies import (
    get_current_company_id,
    require_roles,
)
from app.identity.models.profile import AppRole, Profile
from app.providers.base import (
    LocationProvider,
    Provider,
    SearchableProvider,
    SmsHistoryProvider,
    StatusHistoryProvider,
)
from app.providers.registry import ProviderRegistry
from app.shared.errors import (
    BatchTooLarge,
    IdempotencyKeyRequired,
    ListingPreconditionFailed,
    SubscriptionNotFound,
    UnsupportedOperation,
)
from app.subscriptions.domain import (
    Subscription,
    SubscriptionSearchFilters,
)
from app.subscriptions.models.lifecycle_audit import LifecycleChangeAudit
from app.subscriptions.models.routing import SimRoutingMap
from app.subscriptions.schemas.sim import (
    LocationOut,
    PresenceOut,
    ProviderStatusOut,
    SimDetailsIn,
    SimDetailsItemOut,
    SimDetailsOut,
    SimDetailsSummaryOut,
    SimImportIn,
    SimImportOut,
    SimListOut,
    SimSearchIn,
    SimSearchProviderFilters,
    SimStatsOut,
    SmsHistoryOut,
    SmsHistoryRecordOut,
    StatusChangeIn,
    StatusHistoryOut,
    StatusHistoryRecordOut,
    SubscriptionOut,
    UsageOut,
)
from app.subscriptions.services.credentials import (
    _active_admin_credential_rows,
    _admin_effective_filters,
    _credential_row_company_id,
    _credential_row_provider,
    _load_credentials,
)
from app.subscriptions.services.cursors import decode_cursor, encode_cursor
from app.subscriptions.services.filters import (
    _apply_post_filters,
    _bootstrap_filters_for_provider,  # noqa: F401  (re-exported for test monkeypatching)
    _build_filters,
    _merge_search_filters,
    _parse_custom_filters,
    _parse_metric_list,
    _parse_optional_bool_query,
    _parse_query_dt,
    _search_status_values,
)
from app.subscriptions.services.normalization import (
    _normalized_subscription,
    _parse_any_dt,
    _status_group,
    _to_out,
)
from app.subscriptions.services.provider_dispatch import (
    _adapter_bootstrap_filters,
    _adapter_supports_list_filter,
    _as_exception,
    _details_error_from_exception,
    _global_provider_call_limits,
    _global_provider_failure,
    _is_global_iccid_search,
    _is_searchable_provider,
    _provider_error_fields,
)
from app.subscriptions.services.routing import (
    _discover_iccid_across_providers,  # noqa: F401  (re-exported for test monkeypatching)
    _find_prefix_routing,
    _find_routing,
    _iccid_negative_cache,  # noqa: F401  (re-exported for test monkeypatching)
    _iccid_routing_prefix,  # noqa: F401  (re-exported for test monkeypatching)
    _normalize_iccid_for_routing,
    _resolve_routing_or_discover,
    _routing_iccid,
    _upsert_routing,
)
from app.tenancy.models.idempotency import IdempotencyKey

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/sims", tags=["sims"])
admin_router = APIRouter(
    prefix="/admin/sims",
    tags=["admin-sims"],
    dependencies=[Depends(require_roles(AppRole.admin))],
)
_GLOBAL_CURSOR_PREFIX = "global:"
_ADMIN_CURSOR_PREFIX = "admin:"
_STATUS_CURSOR_PREFIX = "statuses:"
_STATS_PAGE_LIMIT = 500
_STATS_MAX_PAGES = 100


# ── Dependencies ────────────────────────────────────────────────────────────────


def get_registry(request: Request) -> ProviderRegistry:
    registry: ProviderRegistry = request.app.state.provider_registry
    return registry


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


def _require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if idempotency_key is None:
        raise IdempotencyKeyRequired()
    return idempotency_key




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
    subs = _apply_post_filters(subs, filters)

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
    # Cannot use encode_cursor here: global cursor preserves None values so
    # unqueried providers are re-queried on the next page.
    if not provider_cursors:
        return None
    payload = json.dumps(provider_cursors, separators=(",", ":"), sort_keys=True).encode()
    token = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{_GLOBAL_CURSOR_PREFIX}{token}"


def _decode_global_cursor(cursor: str | None) -> dict[str, str | None] | None:
    if cursor is None:
        return None
    if not cursor.startswith(_GLOBAL_CURSOR_PREFIX):
        # Legacy bare cursor: broadcast it to all known providers.
        return {provider.value: cursor for provider in Provider}
    decoded = decode_cursor(_GLOBAL_CURSOR_PREFIX, cursor)
    if not decoded:
        return decoded
    known = {p.value for p in Provider}
    return {k: v for k, v in decoded.items() if k in known}


def _encode_admin_cursor(credential_cursors: dict[str, str | None]) -> str | None:
    return encode_cursor(_ADMIN_CURSOR_PREFIX, credential_cursors)


def _decode_admin_cursor(cursor: str | None) -> dict[str, str | None] | None:
    if cursor is None:
        return None
    # Generic returns None for prefix mismatch; admin callers expect {} instead.
    return decode_cursor(_ADMIN_CURSOR_PREFIX, cursor) or {}


def _admin_cursor_key(
    company_id: uuid.UUID,
    provider_name: str,
    status_key: str | None = None,
) -> str:
    suffix = f":{status_key}" if status_key is not None else ""
    return f"{company_id}:{provider_name}{suffix}"


def _status_cursor_key(status_value: str | None) -> str:
    return status_value or ""


def _encode_status_cursor(status_cursors: dict[str, str | None]) -> str | None:
    return encode_cursor(_STATUS_CURSOR_PREFIX, status_cursors)


def _decode_status_cursor(cursor: str | None) -> dict[str, str | None] | None:
    return decode_cursor(_STATUS_CURSOR_PREFIX, cursor)


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
        if isinstance(result, BaseException):
            failure = _as_exception(result)
            failed_provider, provider_status = _global_provider_failure(
                provider_name, failure
            )
            failed_providers_by_name[provider_name] = failed_provider
            provider_statuses_by_name[provider_name] = provider_status
            logger.warning(
                "global_iccid_search_provider_error",
                provider=provider_name,
                iccid=iccid,
                error=str(failure),
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
        if isinstance(result, BaseException):
            failure = _as_exception(result)
            failed_provider, provider_status = _global_provider_failure(
                provider_name, failure
            )
            failed_providers_by_name[provider_name] = failed_provider
            provider_statuses_by_name[provider_name] = provider_status
            logger.warning(
                "global_listing_provider_error",
                provider=provider_name,
                error=str(failure),
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
        filters,
    ), result in zip(provider_calls, provider_results, strict=True):
        if isinstance(result, BaseException):
            failure = _as_exception(result)
            failed_provider, provider_status = _global_provider_failure(
                provider_name, failure
            )
            provider_errors_by_name[provider_name] = (
                failed_provider,
                provider_status,
            )
            logger.warning(
                "provider_search_error",
                provider=provider_name,
                error=str(failure),
            )
            continue

        raw_subs, next_cursor = result
        subs = _apply_post_filters(raw_subs, filters)
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


async def _list_via_admin_credentials(
    *,
    cursor: str | None,
    limit: int,
    filters: SubscriptionSearchFilters,
    provider: Provider | None,
    db: AsyncSession,
    settings: Settings,
    registry: ProviderRegistry,
) -> SimListOut:
    credential_rows = await _active_admin_credential_rows(db, provider)
    cursor_by_credential = _decode_admin_cursor(cursor)
    page_limit = min(max(limit, 1), 500)
    provider_statuses_by_name: dict[str, ProviderStatusOut] = {}
    failed_providers_by_name: dict[str, dict[str, str]] = {}
    next_credential_cursors: dict[str, str | None] = {}
    provider_success_counts: dict[str, int] = {}
    calls: list[
        tuple[
            str,
            uuid.UUID,
            str,
            Any,
            dict[str, Any],
            str | None,
            int,
            SubscriptionSearchFilters,
        ]
    ] = []

    selected_names = [provider.value] if provider else [p.value for p in Provider]
    if not credential_rows:
        return SimListOut(
            items=[],
            next_cursor=None,
            total=0,
            partial=False,
            failed_providers=[],
            provider_statuses=[
                ProviderStatusOut(
                    provider=provider_name,
                    status="not_queried",
                    title="No active credentials",
                )
                for provider_name in selected_names
            ],
        )

    for row in credential_rows:
        provider_name = _credential_row_provider(row)
        company_id = _credential_row_company_id(row)
        credential_key = _admin_cursor_key(company_id, provider_name)
        if cursor_by_credential is not None and credential_key not in cursor_by_credential:
            provider_statuses_by_name.setdefault(
                provider_name,
                ProviderStatusOut(provider=provider_name, status="not_queried"),
            )
            continue
        provider_cursor = (
            None
            if cursor_by_credential is None
            else cursor_by_credential.get(credential_key)
        )
        try:
            adapter = registry.get(provider_name)
            if not _is_searchable_provider(adapter):
                provider_statuses_by_name[provider_name] = ProviderStatusOut(
                    provider=provider_name,
                    status="not_queried",
                    title="Provider does not expose native listing",
                )
                continue
            creds = await _load_credentials(company_id, provider_name, db, settings)
            calls.append(
                (
                    credential_key,
                    company_id,
                    provider_name,
                    adapter,
                    creds,
                    provider_cursor,
                    0,
                    _admin_effective_filters(
                        provider_name,
                        adapter,
                        filters,
                        use_bootstrap=provider is None,
                    ),
                )
            )
        except Exception as exc:
            failed_provider, provider_status = _global_provider_failure(
                provider_name, exc
            )
            failed_providers_by_name[provider_name] = failed_provider
            provider_statuses_by_name[provider_name] = provider_status
            logger.warning(
                "admin_listing_setup_error",
                provider=provider_name,
                company_id=str(company_id),
                error=str(exc),
            )

    active_calls: list[
        tuple[
            str,
            uuid.UUID,
            str,
            Any,
            dict[str, Any],
            str | None,
            int,
            SubscriptionSearchFilters,
        ]
    ] = []
    for call, call_limit in zip(
        calls,
        _global_provider_call_limits(page_limit, len(calls)),
        strict=True,
    ):
        if call_limit <= 0:
            next_credential_cursors[call[0]] = call[5]
            continue
        active_calls.append((*call[:6], call_limit, call[7]))

    provider_results = await asyncio.gather(
        *(
            adapter.list_subscriptions(
                creds,
                cursor=provider_cursor,
                limit=call_limit,
                filters=effective_filters,
            )
            for (
                _credential_key,
                _company_id,
                _provider_name,
                adapter,
                creds,
                provider_cursor,
                call_limit,
                effective_filters,
            ) in active_calls
        ),
        return_exceptions=True,
    )

    items: list[SubscriptionOut] = []
    seen_items: set[tuple[str, str, str]] = set()
    provider_errors_by_name: dict[str, tuple[dict[str, str], ProviderStatusOut]] = {}
    for (
        credential_key,
        company_id,
        provider_name,
        _adapter,
        _creds,
        _provider_cursor,
        _call_limit,
        effective_filters,
    ), result in zip(active_calls, provider_results, strict=True):
        if isinstance(result, BaseException):
            failure = _as_exception(result)
            provider_errors_by_name[provider_name] = _global_provider_failure(
                provider_name, failure
            )
            logger.warning(
                "admin_listing_error",
                provider=provider_name,
                company_id=str(company_id),
                error=str(failure),
            )
            continue
        raw_subs, next_cursor = result
        subs = _apply_post_filters(raw_subs, effective_filters)
        for sub in subs:
            item_key = (str(sub.company_id), provider_name, sub.iccid)
            if item_key in seen_items:
                continue
            seen_items.add(item_key)
            items.append(_to_out(sub))
            provider_success_counts[provider_name] = (
                provider_success_counts.get(provider_name, 0) + 1
            )
        if next_cursor is not None:
            next_credential_cursors[credential_key] = next_cursor

    for provider_name in selected_names:
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

    failed_providers = [
        failed_providers_by_name[provider_name]
        for provider_name in selected_names
        if provider_name in failed_providers_by_name
    ]
    provider_statuses = [
        provider_statuses_by_name[provider_name]
        for provider_name in selected_names
        if provider_name in provider_statuses_by_name
    ]
    return SimListOut(
        items=items,
        next_cursor=_encode_admin_cursor(next_credential_cursors),
        total=None,
        partial=bool(failed_providers),
        failed_providers=failed_providers,
        provider_statuses=provider_statuses,
    )


async def _search_via_admin_credentials(
    body: SimSearchIn,
    db: AsyncSession,
    settings: Settings,
    registry: ProviderRegistry,
) -> SimListOut:
    selected_providers = body.providers or {
        provider: SimSearchProviderFilters() for provider in Provider
    }
    selected_names = [provider.value for provider in selected_providers]
    cursor_by_credential = _decode_admin_cursor(body.cursor)
    provider_statuses_by_name: dict[str, ProviderStatusOut] = {
        provider.value: ProviderStatusOut(provider=provider.value, status="not_queried")
        for provider in Provider
        if provider not in selected_providers
    }
    failed_providers_by_name: dict[str, dict[str, str]] = {}
    provider_success_counts: dict[str, int] = {}
    next_credential_cursors: dict[str, str | None] = {}
    pending_calls: list[
        tuple[
            str,
            uuid.UUID,
            str,
            Any,
            dict[str, Any],
            str | None,
            str,
            SubscriptionSearchFilters,
        ]
    ] = []

    for provider, provider_filters in selected_providers.items():
        provider_name = provider.value
        credential_rows = await _active_admin_credential_rows(db, provider)
        if not credential_rows:
            provider_statuses_by_name[provider_name] = ProviderStatusOut(
                provider=provider_name,
                status="not_queried",
                title="No active credentials",
            )
            continue
        for row in credential_rows:
            company_id = _credential_row_company_id(row)
            try:
                adapter = registry.get(provider_name)
                if not _is_searchable_provider(adapter):
                    provider_statuses_by_name[provider_name] = ProviderStatusOut(
                        provider=provider_name,
                        status="not_queried",
                        title="Provider does not expose native listing",
                    )
                    continue
                creds = await _load_credentials(company_id, provider_name, db, settings)
                for status_value in _search_status_values(provider_filters):
                    status_key = _status_cursor_key(status_value)
                    credential_key = _admin_cursor_key(
                        company_id, provider_name, status_key
                    )
                    if (
                        cursor_by_credential is not None
                        and credential_key not in cursor_by_credential
                    ):
                        continue
                    pending_calls.append(
                        (
                            credential_key,
                            company_id,
                            provider_name,
                            adapter,
                            creds,
                            None
                            if cursor_by_credential is None
                            else cursor_by_credential.get(credential_key),
                            status_key,
                            _merge_search_filters(
                                body,
                                provider_filters,
                                status_value,
                            ),
                        )
                    )
            except Exception as exc:
                failed_provider, provider_status = _global_provider_failure(
                    provider_name, exc
                )
                failed_providers_by_name[provider_name] = failed_provider
                provider_statuses_by_name[provider_name] = provider_status
                logger.warning(
                    "admin_search_setup_error",
                    provider=provider_name,
                    company_id=str(company_id),
                    error=str(exc),
                )

    call_limits = _global_provider_call_limits(body.limit, len(pending_calls))
    active_calls = [
        (*call, call_limit)
        for call, call_limit in zip(pending_calls, call_limits, strict=True)
        if call_limit > 0
    ]

    provider_results = await asyncio.gather(
        *(
            adapter.list_subscriptions(
                creds,
                cursor=provider_cursor,
                limit=call_limit,
                filters=filters,
            )
            for (
                _credential_key,
                _company_id,
                _provider_name,
                adapter,
                creds,
                provider_cursor,
                _status_key,
                filters,
                call_limit,
            ) in active_calls
        ),
        return_exceptions=True,
    )

    items: list[SubscriptionOut] = []
    seen_items: set[tuple[str, str, str]] = set()
    provider_errors_by_name: dict[str, tuple[dict[str, str], ProviderStatusOut]] = {}
    for (
        credential_key,
        company_id,
        provider_name,
        _adapter,
        _creds,
        _provider_cursor,
        _status_key,
        filters,
        _call_limit,
    ), result in zip(active_calls, provider_results, strict=True):
        if isinstance(result, BaseException):
            failure = _as_exception(result)
            provider_errors_by_name[provider_name] = _global_provider_failure(
                provider_name, failure
            )
            logger.warning(
                "admin_search_error",
                provider=provider_name,
                company_id=str(company_id),
                error=str(failure),
            )
            continue
        raw_subs, next_cursor = result
        subs = _apply_post_filters(raw_subs, filters)
        for sub in subs:
            item_key = (str(sub.company_id), provider_name, sub.iccid)
            if item_key in seen_items:
                continue
            seen_items.add(item_key)
            items.append(_to_out(sub))
            provider_success_counts[provider_name] = (
                provider_success_counts.get(provider_name, 0) + 1
            )
        if next_cursor is not None:
            next_credential_cursors[credential_key] = next_cursor

    for provider_name in selected_names:
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
        next_cursor=_encode_admin_cursor(next_credential_cursors),
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


@admin_router.get("", response_model=SimListOut)
async def admin_list_sims(
    cursor: str | None = None,
    limit: int = 50,
    provider: Provider | None = Query(
        None,
        description="Optional provider filter for the global admin credential fan-out.",
    ),
    status_filter: str | None = Query(None, alias="status"),
    modified_since: str | None = Query(None),
    modified_till: str | None = Query(None),
    iccid: str | None = None,
    imsi: str | None = None,
    msisdn: str | None = None,
    custom: list[str] | None = Query(None),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> SimListOut:
    """List SIMs across all active provider credentials for admins."""
    filters = _build_filters(
        status_filter=status_filter,
        modified_since=modified_since,
        modified_till=modified_till,
        iccid=iccid,
        imsi=imsi,
        msisdn=msisdn,
        custom=custom,
    )
    return await _list_via_admin_credentials(
        cursor=cursor,
        limit=limit,
        filters=filters,
        provider=provider,
        db=db,
        settings=settings,
        registry=registry,
    )


@admin_router.post("/search", response_model=SimListOut)
async def admin_search_sims(
    body: SimSearchIn,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> SimListOut:
    """Search SIMs across all active provider credentials for admins."""
    return await _search_via_admin_credentials(body, db, settings, registry)


def _stats_add_sub(
    stats: dict[str, Any],
    sub: Subscription,
    stale_threshold: datetime,
) -> None:
    stats["total"] += 1
    status_key = sub.status or "UNKNOWN"
    stats["by_status"][status_key] = stats["by_status"].get(status_key, 0) + 1
    group = _status_group(sub.status)
    stats["by_status_group"][group] = stats["by_status_group"].get(group, 0) + 1
    normalized = _normalized_subscription(dataclasses.asdict(sub))
    last_lu = _parse_any_dt((normalized.get("network") or {}).get("last_lu_at"))
    if last_lu is None or last_lu < stale_threshold:
        stats["stale_lu_count"] += 1


async def _collect_provider_stats(
    provider_name: str,
    filters: SubscriptionSearchFilters,
    company_id: uuid.UUID,
    db: AsyncSession,
    settings: Settings,
    registry: ProviderRegistry,
    stale_threshold: datetime,
) -> tuple[dict[str, Any], bool]:
    adapter = registry.get(provider_name)
    if not _is_searchable_provider(adapter):
        raise ListingPreconditionFailed(
            detail=f"Provider '{provider_name}' does not expose native listing.",
            extra={"provider": provider_name, "missing_capability": "SearchableProvider"},
        )
    creds = await _load_credentials(company_id, provider_name, db, settings)
    return await _collect_provider_stats_with_credentials(
        provider_name,
        filters,
        adapter,
        creds,
        stale_threshold,
    )


async def _collect_provider_stats_with_credentials(
    provider_name: str,
    filters: SubscriptionSearchFilters,
    adapter: SearchableProvider,
    creds: dict[str, Any],
    stale_threshold: datetime,
) -> tuple[dict[str, Any], bool]:
    effective_filters = dataclasses.replace(
        _adapter_bootstrap_filters(provider_name, adapter),
        status=filters.status,
        modified_since=filters.modified_since
        or _adapter_bootstrap_filters(provider_name, adapter).modified_since,
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

    cursor: str | None = None
    partial = False
    stats: dict[str, Any] = {
        "total": 0,
        "by_status": {},
        "by_status_group": {},
        "stale_lu_count": 0,
    }
    for page_index in range(_STATS_MAX_PAGES):
        subs, cursor = await adapter.list_subscriptions(
            creds,
            cursor=cursor,
            limit=_STATS_PAGE_LIMIT,
            filters=effective_filters,
        )
        for sub in _apply_post_filters(subs, effective_filters):
            _stats_add_sub(stats, sub, stale_threshold)
        if not cursor:
            break
        if page_index == _STATS_MAX_PAGES - 1:
            partial = True
    return stats, partial


def _merge_stats(base: dict[str, Any], extra: dict[str, Any]) -> None:
    base["total"] += int(extra["total"])
    base["stale_lu_count"] += int(extra["stale_lu_count"])
    for key, value in extra["by_status"].items():
        base["by_status"][key] = base["by_status"].get(key, 0) + value
    for key, value in extra["by_status_group"].items():
        base["by_status_group"][key] = base["by_status_group"].get(key, 0) + value


@router.get("/stats", response_model=SimStatsOut)
async def get_sim_stats(
    provider: Provider | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    modified_since: str | None = None,
    modified_till: str | None = None,
    iccid: str | None = None,
    imsi: str | None = None,
    msisdn: str | None = None,
    imei: str | None = None,
    operator: str | None = None,
    data_service: str | None = None,
    sms_service: str | None = None,
    last_lu_since: str | None = None,
    last_lu_till: str | None = None,
    imsi_list: str | None = None,
    custom: list[str] | None = Query(None),
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> SimStatsOut:
    filters = SubscriptionSearchFilters(
        status=status_filter,
        modified_since=_parse_query_dt(modified_since, "modified_since"),
        modified_till=_parse_query_dt(modified_till, "modified_till"),
        iccid=iccid,
        imsi=imsi,
        msisdn=msisdn,
        imei=imei,
        operator=operator,
        data_service=_parse_optional_bool_query(data_service, "data_service"),
        sms_service=_parse_optional_bool_query(sms_service, "sms_service"),
        last_lu_since=_parse_query_dt(last_lu_since, "last_lu_since"),
        last_lu_till=_parse_query_dt(last_lu_till, "last_lu_till"),
        imsi_list=[item.strip() for item in (imsi_list or "").split(",") if item.strip()]
        or None,
        custom=_parse_custom_filters(custom),
    )
    selected = [provider.value] if provider else [p.value for p in Provider]
    stale_threshold = datetime.now(UTC) - timedelta(days=30)
    merged: dict[str, Any] = {
        "total": 0,
        "by_status": {},
        "by_status_group": {},
        "stale_lu_count": 0,
    }
    failed_providers: list[dict[str, str]] = []
    partial = False
    for provider_name in selected:
        try:
            provider_stats, provider_partial = await _collect_provider_stats(
                provider_name,
                filters,
                company_id,
                db,
                settings,
                registry,
                stale_threshold,
            )
            _merge_stats(merged, provider_stats)
            partial = partial or provider_partial
        except Exception as exc:
            failed_provider, _provider_status = _global_provider_failure(
                provider_name, _as_exception(exc)
            )
            failed_providers.append(failed_provider)
            partial = True
            logger.warning(
                "sim_stats_provider_error",
                provider=provider_name,
                error=str(exc),
            )
    return SimStatsOut(
        total=merged["total"],
        by_status=merged["by_status"],
        by_status_group=merged["by_status_group"],
        stale_lu_count=merged["stale_lu_count"],
        provider=provider.value if provider else None,
        fresh_at=datetime.now(UTC),
        partial=partial,
        failed_providers=failed_providers,
    )


@admin_router.get("/stats", response_model=SimStatsOut)
async def admin_get_sim_stats(
    provider: Provider | None = Query(None),
    status_filter: str | None = Query(None, alias="status"),
    modified_since: str | None = None,
    modified_till: str | None = None,
    iccid: str | None = None,
    imsi: str | None = None,
    msisdn: str | None = None,
    imei: str | None = None,
    operator: str | None = None,
    data_service: str | None = None,
    sms_service: str | None = None,
    last_lu_since: str | None = None,
    last_lu_till: str | None = None,
    imsi_list: str | None = None,
    custom: list[str] | None = Query(None),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> SimStatsOut:
    filters = SubscriptionSearchFilters(
        status=status_filter,
        modified_since=_parse_query_dt(modified_since, "modified_since"),
        modified_till=_parse_query_dt(modified_till, "modified_till"),
        iccid=iccid,
        imsi=imsi,
        msisdn=msisdn,
        imei=imei,
        operator=operator,
        data_service=_parse_optional_bool_query(data_service, "data_service"),
        sms_service=_parse_optional_bool_query(sms_service, "sms_service"),
        last_lu_since=_parse_query_dt(last_lu_since, "last_lu_since"),
        last_lu_till=_parse_query_dt(last_lu_till, "last_lu_till"),
        imsi_list=[item.strip() for item in (imsi_list or "").split(",") if item.strip()]
        or None,
        custom=_parse_custom_filters(custom),
    )
    selected_names = [provider.value] if provider else [p.value for p in Provider]
    stale_threshold = datetime.now(UTC) - timedelta(days=30)
    merged: dict[str, Any] = {
        "total": 0,
        "by_status": {},
        "by_status_group": {},
        "stale_lu_count": 0,
    }
    failed_providers: list[dict[str, str]] = []
    partial = False

    for provider_name in selected_names:
        rows = await _active_admin_credential_rows(db, Provider(provider_name))
        if not rows:
            continue
        for row in rows:
            company_id = _credential_row_company_id(row)
            try:
                adapter = registry.get(provider_name)
                if not _is_searchable_provider(adapter):
                    raise ListingPreconditionFailed(
                        detail=f"Provider '{provider_name}' does not expose native listing.",
                        extra={
                            "provider": provider_name,
                            "missing_capability": "SearchableProvider",
                        },
                    )
                creds = await _load_credentials(company_id, provider_name, db, settings)
                provider_stats, provider_partial = (
                    await _collect_provider_stats_with_credentials(
                        provider_name,
                        filters,
                        adapter,
                        creds,
                        stale_threshold,
                    )
                )
                _merge_stats(merged, provider_stats)
                partial = partial or provider_partial
            except Exception as exc:
                failed_provider, _provider_status = _global_provider_failure(
                    provider_name, _as_exception(exc)
                )
                failed_providers.append(failed_provider)
                partial = True
                logger.warning(
                    "admin_sim_stats_provider_error",
                    provider=provider_name,
                    company_id=str(company_id),
                    error=str(exc),
                )

    return SimStatsOut(
        total=merged["total"],
        by_status=merged["by_status"],
        by_status_group=merged["by_status_group"],
        stale_lu_count=merged["stale_lu_count"],
        provider=provider.value if provider else None,
        fresh_at=datetime.now(UTC),
        partial=partial,
        failed_providers=failed_providers,
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

    detail_limit = max(int(settings.max_detail_concurrent_requests), 1)
    detail_sem = asyncio.Semaphore(detail_limit)

    async def _fetch_one_limited(
        item: tuple[str, str, str],
    ) -> tuple[str, SimDetailsItemOut]:
        async with detail_sem:
            return await _fetch_one(*item)

    fetched = await asyncio.gather(*(_fetch_one_limited(item) for item in resolved))
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


@router.get("/{iccid}/status-history", response_model=StatusHistoryOut)
async def get_status_history(
    iccid: str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> StatusHistoryOut:
    routing, _ = await _resolve_routing_or_discover(
        iccid, company_id, db, settings, registry
    )
    adapter = registry.get(routing.provider)
    if not isinstance(adapter, StatusHistoryProvider):
        raise UnsupportedOperation(
            detail=f"Provider '{routing.provider}' does not expose status history"
        )
    creds = await _load_credentials(company_id, routing.provider, db, settings)
    routed_iccid = _routing_iccid(routing, iccid)
    records = await adapter.get_status_history(
        routed_iccid, creds, start_date=start_date, end_date=end_date
    )
    return StatusHistoryOut(
        iccid=iccid,
        period_start=start_date,
        period_end=end_date,
        records=[StatusHistoryRecordOut(**dataclasses.asdict(r)) for r in records],
    )


@router.get("/{iccid}/location", response_model=LocationOut)
async def get_location(
    iccid: str,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> LocationOut:
    routing, _ = await _resolve_routing_or_discover(
        iccid, company_id, db, settings, registry
    )
    adapter = registry.get(routing.provider)
    if not isinstance(adapter, LocationProvider):
        raise UnsupportedOperation(
            detail=f"Provider '{routing.provider}' does not expose location detail"
        )
    creds = await _load_credentials(company_id, routing.provider, db, settings)
    routed_iccid = _routing_iccid(routing, iccid)
    location = await adapter.get_location(routed_iccid, creds)
    return LocationOut(**dataclasses.asdict(location))


@router.get("/{iccid}/sms-history", response_model=SmsHistoryOut)
async def get_sms_history(
    iccid: str,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    company_id: uuid.UUID = Depends(get_current_company_id),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    registry: ProviderRegistry = Depends(get_registry),
) -> SmsHistoryOut:
    routing, _ = await _resolve_routing_or_discover(
        iccid, company_id, db, settings, registry
    )
    adapter = registry.get(routing.provider)
    if not isinstance(adapter, SmsHistoryProvider):
        raise UnsupportedOperation(
            detail=f"Provider '{routing.provider}' does not expose SMS history"
        )

    now = datetime.now(tz=UTC)
    end = end_date or now
    start = start_date or (end - timedelta(days=30))

    creds = await _load_credentials(company_id, routing.provider, db, settings)
    routed_iccid = _routing_iccid(routing, iccid)
    records = await adapter.get_sms_history(
        routed_iccid, creds, start_date=start, end_date=end
    )
    return SmsHistoryOut(
        iccid=iccid,
        period_start=start,
        period_end=end,
        records=[SmsHistoryRecordOut(**dataclasses.asdict(r)) for r in records],
    )


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
