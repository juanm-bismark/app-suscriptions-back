"""Provider capability checks and dispatch helpers."""
import asyncio
from typing import Any, Literal, TypeGuard

from app.providers.base import Provider, SearchableProvider
from app.shared.errors import (
    DomainError,
    ProviderRateLimited,
    ProviderResourceNotFound,
    ProviderUnavailable,
    SubscriptionNotFound,
)
from app.subscriptions.domain import SubscriptionSearchFilters
from app.subscriptions.schemas.sim import ProviderStatusOut, SimDetailsErrorOut
from app.subscriptions.services.filters import _bootstrap_filters_for_provider


def _is_searchable_provider(adapter: Any) -> TypeGuard[SearchableProvider]:
    return callable(getattr(adapter, "list_subscriptions", None))


def _as_exception(exc: BaseException) -> Exception:
    if isinstance(exc, Exception):
        return exc
    return RuntimeError(str(exc))


def _adapter_bootstrap_filters(
    provider_name: str,
    adapter: Any,
) -> SubscriptionSearchFilters:
    from typing import cast
    bootstrap_filters = getattr(adapter, "bootstrap_filters", None)
    if callable(bootstrap_filters):
        return cast(SubscriptionSearchFilters, bootstrap_filters())
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


def _provider_error_fields(exc: Exception) -> tuple[str | None, str | None]:
    if isinstance(exc, DomainError):
        return (
            exc.extra.get("provider_request_id") or exc.extra.get("transaction_id"),
            exc.extra.get("provider_error_code") or exc.extra.get("exception_id"),
        )
    return None, None


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
