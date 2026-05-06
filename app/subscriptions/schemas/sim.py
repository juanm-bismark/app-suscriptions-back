"""Pydantic response schemas for the Subscriptions API.

Canonical vocabulary for status values (AdministrativeStatus):
  active      → SIM is provisioned and billable (Kite: ACTIVE, Tele2: ACTIVE/ACTIVATED, Moabits: Active)
  in_test     → SIM in trial/test mode before production activation
                (Kite: TEST, Tele2: Ready, Moabits: Ready)
  suspended   → SIM service temporarily blocked (Kite: SUSPENDED, Moabits: Suspended)
  terminated  → SIM deactivated / end-of-life (Kite: DEACTIVATED, Tele2: DEACTIVATED)
  purged      → SIM removed from network (Tele2: PURGED, Moabits: PURGED)
  pending     → Provisioned but not yet activated (Kite: PENDING)
  unknown     → Provider returned an unrecognized value

The `native_status` field always carries the raw value from the provider and is intended
to be shown as a tooltip or subtitle in the UI alongside the unified `status` label.

The `provider_fields` dict is a dynamic block of provider-specific attributes. Its shape
varies by provider — see adapter docstrings for field documentation.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from app.subscriptions.domain import AdministrativeStatus


class SubscriptionOut(BaseModel):
    iccid: str
    msisdn: str | None
    imsi: str | None
    status: AdministrativeStatus  # unified canonical value as enum
    native_status: str            # raw provider value (e.g. "ACTIVE", "Ready", "TEST") — tooltip source
    provider: str
    company_id: str
    activated_at: datetime | None
    updated_at: datetime | None
    provider_fields: dict[str, Any]  # dynamic provider-specific block

    model_config = {"from_attributes": True}


class UsageMetricOut(BaseModel):
    metric_type: str
    usage: Decimal
    unit: str | None


class UsageOut(BaseModel):
    iccid: str
    period_start: datetime
    period_end: datetime
    data_used_bytes: Decimal
    sms_count: int
    voice_seconds: int
    provider_metrics: dict[str, Any] = Field(default_factory=dict)
    usage_metrics: list[UsageMetricOut] = Field(default_factory=list)

    model_config = {"from_attributes": True}


class PresenceOut(BaseModel):
    iccid: str
    state: str           # "online" | "offline" | "unknown"
    ip_address: str | None
    country_code: str | None
    rat_type: str | None
    network_name: str | None
    last_seen_at: datetime | None

    model_config = {"from_attributes": True}


class SimListOut(BaseModel):
    items: list[SubscriptionOut]
    next_cursor: str | None
    total: int | None = None
    partial: bool = False
    failed_providers: list[dict[str, str]] = Field(default_factory=list)


class StatusChangeIn(BaseModel):
    target: str  # canonical AdministrativeStatus value (e.g. "active", "suspended")
    data_service: bool | None = None  # For providers that support selective service control (Moabits)
    sms_service: bool | None = None   # For providers that support selective service control (Moabits)


class SimImportItem(BaseModel):
    iccid: str
    provider: str


class SimImportIn(BaseModel):
    sims: list[SimImportItem]


class SimImportOut(BaseModel):
    imported: int
