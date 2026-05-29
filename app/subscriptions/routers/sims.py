"""SIM management API endpoints.

All endpoints are scoped to the caller's company via their JWT profile.
The routing table (SimRoutingMap) maps iccid → provider + company_id and is
populated lazily on first successful list, or explicitly via POST /import.
"""

import asyncio
import dataclasses
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
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
    SmsHistoryProvider,
    StatusHistoryProvider,
)
from app.providers.registry import ProviderRegistry
from app.shared.errors import (
    BatchTooLarge,
    ListingPreconditionFailed,
    SubscriptionNotFound,
    UnsupportedOperation,
)
from app.subscriptions.domain import (
    SubscriptionSearchFilters,
)
from app.subscriptions.models.routing import SimRoutingMap
from app.subscriptions.schemas.sim import (
    LocationOut,
    PresenceOut,
    SimDetailsIn,
    SimDetailsItemOut,
    SimDetailsOut,
    SimDetailsSummaryOut,
    SimImportIn,
    SimImportOut,
    SimListOut,
    SimSearchIn,
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
    _credential_row_company_id,
    _load_credentials,
)
from app.subscriptions.services.filters import (
    _build_filters,
    _parse_custom_filters,
    _parse_metric_list,
    _parse_optional_bool_query,
    _parse_query_dt,
)
from app.subscriptions.services.idempotency import (
    _claim_idempotency_key,
    _mark_idempotency_processed,
    _release_idempotency_key,
    _require_idempotency_key,
    _write_lifecycle_audit,
)
from app.subscriptions.services.listing import (
    _list_via_admin_credentials,
    _list_via_provider_search,
    _list_via_routing_index,
    _search_via_admin_credentials,
    _search_via_provider_filters,
    _tele2_missing_modified_since_response,
)
from app.subscriptions.services.normalization import (
    _to_out,
)
from app.subscriptions.services.provider_dispatch import (
    _as_exception,
    _details_error_from_exception,
    _global_provider_failure,
    _is_searchable_provider,
    _provider_error_fields,
)
from app.subscriptions.services.routing import (
    _find_routing,
    _resolve_routing_or_discover,
    _routing_iccid,
    _upsert_routing,
)
from app.subscriptions.services.stats import (
    _collect_provider_stats,
    _collect_provider_stats_with_credentials,
    _merge_stats,
)

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


def _cursor_has_modified_since(cursor: str | None) -> bool:
    if not cursor:
        return False
    return any(part.startswith("since:") for part in cursor.split("|"))



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
