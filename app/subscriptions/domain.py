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


class AdministrativeStatus(StrEnum):
    ACTIVE = "active"
    IN_TEST = "in_test"       # Kite: TEST | Tele2: Ready | Moabits: Ready
    SUSPENDED = "suspended"
    INACTIVE_NEW = "inactive_new"
    ACTIVATION_PENDANT = "activation_pendant"
    ACTIVATION_READY = "activation_ready"
    TERMINATED = "terminated"
    PURGED = "purged"         # Tele2: PURGED | Moabits: PURGED
    INVENTORY = "inventory"
    REPLACED = "replaced"
    RETIRED = "retired"
    RESTORE = "restore"
    PENDING = "pending"
    UNKNOWN = "unknown"


class ConnectivityState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Subscription:
    iccid: str
    msisdn: str | None
    imsi: str | None
    status: AdministrativeStatus
    native_status: str          # raw provider value — shown as tooltip/subtitle in UI
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
class SubscriptionSearchFilters:
    status: AdministrativeStatus | None = None
    modified_since: datetime | None = None
    modified_till: datetime | None = None
    iccid: str | None = None
    imsi: str | None = None
    msisdn: str | None = None
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
                bool(self.custom),
            )
        )
