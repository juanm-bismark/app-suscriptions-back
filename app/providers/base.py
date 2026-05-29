"""Provider Protocol, capability protocols, and Provider enum.

`SubscriptionProvider` is the **required** core interface — every adapter
must implement it. Optional capabilities are exposed as separate
runtime-checkable protocols (currently `SearchableProvider`) so the router
can dispatch via `isinstance(adapter, Capability)` and adapters that don't
expose a feature do not have to implement a no-op stub.

Credentials are passed as a plain dict per-call; each adapter converts
them internally to its own typed credentials dataclass. The dict shape per
provider is documented in each adapter module.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.subscriptions.domain import (
    ConnectivityPresence,
    LocationDetail,
    SmsHistoryRecord,
    StatusHistoryRecord,
    Subscription,
    SubscriptionSearchFilters,
    UsageSnapshot,
)


class Provider(StrEnum):
    KITE = "kite"
    TELE2 = "tele2"
    MOABITS = "moabits"


@runtime_checkable
class SubscriptionProvider(Protocol):
    """Required core interface — one singleton per provider in the registry."""

    async def get_subscription(
        self, iccid: str, credentials: dict[str, Any]
    ) -> Subscription: ...

    async def get_usage(
        self,
        iccid: str,
        credentials: dict[str, Any],
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        metrics: list[str] | None = None,
    ) -> UsageSnapshot: ...

    async def get_presence(
        self, iccid: str, credentials: dict[str, Any]
    ) -> ConnectivityPresence: ...

    async def set_administrative_status(
        self,
        iccid: str,
        credentials: dict[str, Any],
        *,
        target: str,
        idempotency_key: str,
        data_service: bool | None = None,
        sms_service: bool | None = None,
    ) -> None: ...

    async def purge(
        self, iccid: str, credentials: dict[str, Any], *, idempotency_key: str
    ) -> None: ...


@runtime_checkable
class SearchableProvider(Protocol):
    """Optional capability — adapters that expose a native subscription listing.

    The router delegates `GET /v1/sims?provider=<name>` to this method when
    available, preserving the provider's native cursor and limit semantics.

    Scope is implicit in the credentials: each `(Company, Provider)` credential
    record already addresses a single tenant's account on the provider side, so
    the listing returns only that account's SIMs. There is no `company_id`
    parameter — the credentials are the scope.

    Adapters that do not implement this protocol fall through to the local
    SimRoutingMap-based listing path.
    """

    async def list_subscriptions(
        self,
        credentials: dict[str, Any],
        *,
        cursor: str | None,
        limit: int,
        filters: SubscriptionSearchFilters | None = None,
    ) -> tuple[list[Subscription], str | None]: ...

    def supports_list_filter(self, filter_name: str) -> bool: ...

    def bootstrap_filters(self) -> SubscriptionSearchFilters: ...


@runtime_checkable
class SmsHistoryProvider(Protocol):
    """Optional capability — adapters that expose SMS log/history.

    Currently only Moabits implements this. The router dispatches via
    `isinstance(adapter, SmsHistoryProvider)`; adapters that do not implement
    this protocol cause the endpoint to return 501 / NOT_SUPPORTED.

    Adapters whose underlying provider exposes SMS history at the *account*
    level (not per-ICCID) must filter the results to the requested ICCID
    before returning. Records returned are always pre-filtered for the SIM.
    """

    async def get_sms_history(
        self,
        iccid: str,
        credentials: dict[str, Any],
        *,
        start_date: datetime,
        end_date: datetime,
    ) -> list[SmsHistoryRecord]: ...


@runtime_checkable
class StatusHistoryProvider(Protocol):
    """Optional capability — adapters that expose lifecycle/status history."""

    async def get_status_history(
        self,
        iccid: str,
        credentials: dict[str, Any],
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[StatusHistoryRecord]: ...


@runtime_checkable
class LocationProvider(Protocol):
    """Optional capability — adapters that expose SIM/device location detail."""

    async def get_location(
        self,
        iccid: str,
        credentials: dict[str, Any],
    ) -> LocationDetail: ...
