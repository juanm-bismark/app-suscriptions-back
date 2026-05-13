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
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.subscriptions.domain import AdministrativeStatus


class SubscriptionOut(BaseModel):
    iccid: str = Field(description="Canonical SIM ICCID.")
    msisdn: str | None = Field(
        default=None,
        description=(
            "SIM MSISDN when the provider response includes it. Summary listing "
            "responses may omit this even when the SIM has an MSISDN."
        ),
    )
    imsi: str | None = Field(
        default=None,
        description=(
            "SIM IMSI when the provider response includes it. Summary listing "
            "responses may omit this even when the SIM has an IMSI."
        ),
    )
    status: AdministrativeStatus = Field(
        description="Unified internal administrative status."
    )
    # raw provider value (e.g. "ACTIVE", "Ready", "TEST") — tooltip source
    native_status: str = Field(description="Raw provider status value.")
    provider: str = Field(description="Provider that owns this SIM route.")
    company_id: str
    activated_at: datetime | None = Field(
        default=None,
        description="Best available activation date mapped from the provider.",
    )
    updated_at: datetime | None = Field(
        default=None,
        description="Best available update/state-change date mapped from the provider.",
    )
    provider_fields: dict[str, Any] = Field(
        description=(
            "Provider-specific fields kept for advanced/provider-native views. "
            "Consumers should prefer `normalized` for common UI fields."
        )
    )
    detail_level: Literal["summary", "detail"] = Field(
        default="detail",
        description=(
            "`detail` means the row was enriched with the provider detail endpoint. "
            "`summary` means it came from a lighter listing response."
        ),
    )
    normalized: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Provider-neutral blocks for common UI consumption: identity, status, "
            "plan, customer, network, hardware, services, limits, dates, and "
            "custom_fields."
        ),
    )

    model_config = {
        "from_attributes": True,
        "json_schema_extra": {
            "examples": [
                {
                    "iccid": "89462038075065380465",
                    "msisdn": None,
                    "imsi": None,
                    "status": "active",
                    "native_status": "ACTIVATED",
                    "provider": "tele2",
                    "company_id": "00000000-0000-0000-0000-000000000001",
                    "activated_at": None,
                    "updated_at": None,
                    "detail_level": "summary",
                    "provider_fields": {
                        "rate_plan": "PAYU - BISMARK",
                        "communication_plan": "Data LTE SMS VoLTE",
                    },
                    "normalized": {
                        "identity": {
                            "iccid": "89462038075065380465",
                            "msisdn": None,
                            "imsi": None,
                            "imei": None,
                        },
                        "status": {
                            "value": "active",
                            "native": "ACTIVATED",
                            "last_changed_at": None,
                        },
                        "plan": {
                            "name": "PAYU - BISMARK",
                            "communication_plan": "Data LTE SMS VoLTE",
                        },
                        "customer": {"account_id": None},
                        "network": {"ip_address": None},
                        "hardware": {"device_id": None, "modem_id": None},
                        "services": {"active": None},
                        "limits": {"data": None, "sms": None},
                        "dates": {"activated_at": None, "updated_at": None},
                        "custom_fields": {},
                    },
                },
                {
                    "iccid": "8988216716970004975",
                    "msisdn": "882351697004975",
                    "imsi": "901161697004975",
                    "status": "active",
                    "native_status": "ACTIVATED",
                    "provider": "tele2",
                    "company_id": "00000000-0000-0000-0000-000000000001",
                    "activated_at": "2016-06-29T00:21:33.339000Z",
                    "updated_at": "2016-07-06T22:04:04.380000Z",
                    "detail_level": "detail",
                    "provider_fields": {
                        "rate_plan": "hphlr rp1",
                        "communication_plan": "CP_Basic_ON",
                        "imei": "12345",
                        "account_id": "100020620",
                        "modem_id": "2221",
                        "account_custom_1": "78",
                        "detail_enriched": True,
                    },
                    "normalized": {
                        "identity": {
                            "iccid": "8988216716970004975",
                            "msisdn": "882351697004975",
                            "imsi": "901161697004975",
                            "imei": "12345",
                        },
                        "status": {
                            "value": "active",
                            "native": "ACTIVATED",
                            "last_changed_at": "2016-07-06T22:04:04.380000Z",
                        },
                        "plan": {
                            "name": "hphlr rp1",
                            "communication_plan": "CP_Basic_ON",
                        },
                        "customer": {"account_id": "100020620"},
                        "network": {"ip_address": None},
                        "hardware": {"modem_id": "2221"},
                        "services": {"active": None},
                        "limits": {"data": None, "sms": None},
                        "dates": {
                            "activated_at": "2016-06-29T00:21:33.339000Z",
                            "updated_at": "2016-07-06T22:04:04.380000Z",
                        },
                        "custom_fields": {"account_custom_1": "78"},
                    },
                },
            ]
        },
    }


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
    state: str  # "online" | "offline" | "unknown"
    ip_address: str | None
    country_code: str | None
    rat_type: str | None
    network_name: str | None
    last_seen_at: datetime | None

    model_config = {"from_attributes": True}


class ProviderStatusOut(BaseModel):
    provider: str = Field(description="Provider name for this listing source.")
    status: Literal["ok", "partial", "error", "not_queried"] = Field(
        description=(
            "Provider listing result. `ok` means the source was queried "
            "successfully, `partial` means it returned usable data with caveats, "
            "`error` means the source failed, and `not_queried` means the source "
            "was skipped for this request."
        )
    )
    count: int = Field(
        default=0,
        description="Number of SIM rows contributed by this provider.",
    )
    code: str | None = Field(
        default=None,
        description="Machine-readable provider or domain error code when present.",
    )
    title: str | None = Field(
        default=None,
        description="Human-readable provider status or error summary.",
    )


class SimListOut(BaseModel):
    items: list[SubscriptionOut] = Field(
        description=(
            "SIMs in normalized response format. Tele2 provider listings enrich "
            "at most the first 5 rows with details per page."
        )
    )
    next_cursor: str | None = Field(
        default=None,
        description="Cursor for the next page, preserving provider pagination state.",
    )
    total: int | None = Field(
        default=None,
        description=(
            "Known total when available. Provider-scoped live listings may not "
            "provide a total."
        ),
    )
    partial: bool = Field(
        default=False,
        description="True when a multi-provider/global listing returned partial data.",
    )
    failed_providers: list[dict[str, str]] = Field(
        default_factory=list,
        description="Provider failures captured during partial global listings.",
    )
    provider_statuses: list[ProviderStatusOut] = Field(
        default_factory=list,
        description=(
            "Per-provider listing metadata for global listings, useful for "
            "distinguishing successful zero-result providers from skipped or "
            "failed providers."
        ),
    )


class StatusChangeIn(BaseModel):
    target: str  # canonical AdministrativeStatus value (e.g. "active", "suspended")
    data_service: bool | None = (
        None  # For providers that support selective service control (Moabits)
    )
    sms_service: bool | None = (
        None  # For providers that support selective service control (Moabits)
    )


class SimImportItem(BaseModel):
    iccid: str
    provider: str


class SimImportIn(BaseModel):
    sims: list[SimImportItem]


class SimImportOut(BaseModel):
    imported: int
