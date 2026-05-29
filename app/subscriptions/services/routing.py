"""ICCID-to-provider routing resolution with negative-cache and discovery."""
import asyncio
import dataclasses
import re
import time
import uuid
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.providers.base import Provider
from app.providers.registry import ProviderRegistry
from app.shared.errors import SubscriptionNotFound
from app.subscriptions.domain import Subscription
from app.subscriptions.models.routing import SimRoutingMap, SimRoutingPrefixMap
from app.subscriptions.services.credentials import _load_credentials
from app.subscriptions.services.provider_dispatch import (
    _adapter_bootstrap_filters,
    _adapter_supports_list_filter,
    _is_searchable_provider,
)

logger = structlog.get_logger(__name__)

_ICCID_DIGITS_RE = re.compile(r"\D+")
_ICCID_ROUTING_PREFIX_LENGTH = 6

_NEGATIVE_CACHE_TTL_SECONDS = 60.0
_NEGATIVE_CACHE_MAX_ENTRIES = 10_000
_iccid_negative_cache: dict[tuple[uuid.UUID, str], float] = {}


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
        if isinstance(result, BaseException):
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
