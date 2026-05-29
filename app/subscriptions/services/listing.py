"""Listing and search helpers extracted from the SIM router.

Contains the fan-out / routing-index listing functions so they can be tested
and reused independently of the FastAPI router layer.
"""

import asyncio
import base64
import dataclasses
import json
import uuid
from typing import Any

import structlog
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.providers.base import Provider
from app.providers.registry import ProviderRegistry
from app.shared.errors import ListingPreconditionFailed, UnsupportedOperation
from app.subscriptions.domain import SubscriptionSearchFilters
from app.subscriptions.schemas.sim import (
    ProviderStatusOut,
    SimListOut,
    SimSearchIn,
    SimSearchProviderFilters,
    SubscriptionOut,
)
from app.subscriptions.services.credentials import (
    _active_admin_credential_rows,
    _admin_effective_filters,
    _credential_row_company_id,
    _credential_row_provider,
    _load_credentials,
)
from app.subscriptions.services.cursors import (
    _ADMIN_CURSOR_PREFIX,
    _GLOBAL_CURSOR_PREFIX,
    _STATUS_CURSOR_PREFIX,
    decode_cursor,
    encode_cursor,
)
from app.subscriptions.services.filters import (
    _apply_post_filters,
    _merge_search_filters,
    _search_status_values,
)
from app.subscriptions.services.normalization import _to_out
from app.subscriptions.services.provider_dispatch import (
    _adapter_bootstrap_filters,
    _adapter_supports_list_filter,
    _as_exception,
    _global_provider_call_limits,
    _global_provider_failure,
    _is_global_iccid_search,
    _is_searchable_provider,
)
from app.subscriptions.services.routing import (
    _find_prefix_routing,
    _find_routing,
    _normalize_iccid_for_routing,
    _routing_iccid,
    _upsert_routing,
)

logger = structlog.get_logger(__name__)


def _tele2_missing_modified_since_response() -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "errorMessage": "ModifiedSince is required.",
            "errorCode": "10000003",
        },
    )


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
