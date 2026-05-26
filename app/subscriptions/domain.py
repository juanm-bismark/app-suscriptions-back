"""Canonical domain types for the Subscriptions bounded context.

All provider adapters must map their own vocabulary to these types.
Provider-specific terms (icc, lifeCycleStatus, simStatus, …) must never
appear outside of app/providers/<provider>/.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, cast


def _provider_metrics_default() -> dict[str, Any]:
    return {}


class ConnectivityState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Subscription:
    iccid: str
    msisdn: str | None
    imsi: str | None
    status: str                 # raw provider value (e.g. "ACTIVE", "ACTIVATED", "Active")
    provider: str
    company_id: str
    activated_at: datetime | None
    updated_at: datetime | None
    provider_fields: dict[str, Any] = field(default_factory=lambda: cast(dict[str, Any], {}))


@dataclass(frozen=True)
class UsageMetric:
    metric_type: str
    usage: Decimal
    unit: str | None


@dataclass(frozen=True)
class UsageSnapshot:
    iccid: str
    period_start: datetime
    period_end: datetime
    data_used_bytes: Decimal
    sms_count: int
    voice_seconds: int
    provider_metrics: dict[str, Any] = field(default_factory=_provider_metrics_default)
    usage_metrics: list[UsageMetric] = field(default_factory=lambda: cast(list[UsageMetric], []))

    @property
    def data_used_mb(self) -> Decimal:
        """Deprecated compatibility alias. The value is now bytes."""
        return self.data_used_bytes

    @property
    def sms_sent(self) -> int:
        """Deprecated compatibility alias for the canonical SMS count."""
        return self.sms_count

    @property
    def voice_minutes(self) -> int:
        """Deprecated compatibility alias. The value is now seconds."""
        return self.voice_seconds


@dataclass(frozen=True)
class ConnectivityPresence:
    iccid: str
    state: ConnectivityState
    ip_address: str | None
    country_code: str | None
    rat_type: str | None
    network_name: str | None
    last_seen_at: datetime | None


@dataclass(frozen=True)
class LocationDetail:
    """Manual/automatic provider location for a subscription."""
    iccid: str
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    accuracy_m: Decimal | None = None
    timestamp: datetime | None = None
    source: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StatusDetail:
    """Current status of a subscription with reason and timestamp."""
    iccid: str
    state: str
    automatic: bool
    current_status_date: datetime
    change_reason: str | None = None
    user: str | None = None


@dataclass(frozen=True)
class StatusHistoryRecord:
    """A single entry in the subscription status history."""
    state: str
    automatic: bool
    time: datetime
    reason: str | None = None
    user: str | None = None


@dataclass(frozen=True)
class SmsHistoryRecord:
    """A single SMS history entry returned by a provider's SMS log API."""
    iccid: str
    date: datetime
    message: str
    sms_type: str  # "MO" | "MT"
    gateway_delivered: bool | None = None
    sms_center_delivered: bool | None = None


@dataclass(frozen=True)
class SubscriptionSearchFilters:
    status: str | None = None
    modified_since: datetime | None = None
    modified_till: datetime | None = None
    iccid: str | None = None
    imsi: str | None = None
    msisdn: str | None = None
    imei: str | None = None
    operator: str | None = None
    data_service: bool | None = None
    sms_service: bool | None = None
    last_lu_since: datetime | None = None
    last_lu_till: datetime | None = None
    imsi_list: list[str] | None = None
    custom: dict[str, str] = field(default_factory=dict)

    @property
    def has_filters(self) -> bool:
        return any(
            (
                self.status is not None,
                self.modified_since is not None,
                self.modified_till is not None,
                bool(self.iccid),
                bool(self.imsi),
                bool(self.msisdn),
                bool(self.imei),
                bool(self.operator),
                self.data_service is not None,
                self.sms_service is not None,
                self.last_lu_since is not None,
                self.last_lu_till is not None,
                bool(self.imsi_list),
                bool(self.custom),
            )
        )
