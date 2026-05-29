"""SIM statistics collection and aggregation helpers."""
import dataclasses
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.providers.base import SearchableProvider
from app.providers.registry import ProviderRegistry
from app.shared.errors import ListingPreconditionFailed
from app.subscriptions.domain import Subscription, SubscriptionSearchFilters
from app.subscriptions.services.credentials import _load_credentials
from app.subscriptions.services.filters import _apply_post_filters
from app.subscriptions.services.normalization import (
    _normalized_subscription,
    _parse_any_dt,
    _status_group,
)
from app.subscriptions.services.provider_dispatch import (
    _adapter_bootstrap_filters,
    _is_searchable_provider,
)

_STATS_PAGE_LIMIT = 500
_STATS_MAX_PAGES = 100


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
