"""Pydantic response schemas for the Subscriptions API.

The `status` field carries the raw native value from the provider
(e.g. "ACTIVE" for Kite, "ACTIVATED" for Tele2, "Active" for Moabits).

The `provider_fields` dict is a dynamic block of provider-specific attributes. Its shape
varies by provider — see adapter docstrings for field documentation.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.providers.base import Provider


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
    status: str = Field(
        description="Raw provider status value (e.g. 'ACTIVE', 'ACTIVATED', 'Active')."
    )
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
                    "status": "ACTIVATED",
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
                            "imei": None,
                            "alias": None,
                            "eid": None,
                            "euiccid": None,
                            "sim_profile_id": None,
                        },
                        "status": {
                            "label": "Activated",
                            "group": "active_like",
                            "group_label": "Active-like",
                            "source": "provider",
                            "last_changed_at": None,
                        },
                        "plan": {
                            "name": "PAYU - BISMARK",
                            "communication_plan": "Data LTE SMS VoLTE",
                        },
                        "customer": {"account_id": None},
                        "network": {
                            "ip_address": None,
                            "fixed_ip_address": None,
                            "ipv6_address": None,
                        },
                        "hardware": {"device_id": None, "modem_id": None},
                        "services": {"active": None},
                        "limits": {"data": None, "sms": None},
                        "dates": {"added_at": None, "provisioned_at": None},
                        "custom_fields": {},
                    },
                },
                {
                    "iccid": "8988216716970004975",
                    "msisdn": "882351697004975",
                    "imsi": "901161697004975",
                    "status": "ACTIVATED",
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
                            "imei": "12345",
                            "alias": None,
                            "eid": None,
                            "euiccid": None,
                            "sim_profile_id": None,
                        },
                        "status": {
                            "label": "Activated",
                            "group": "active_like",
                            "group_label": "Active-like",
                            "source": "provider",
                            "last_changed_at": "2016-07-06T22:04:04.380000Z",
                        },
                        "plan": {
                            "name": "hphlr rp1",
                            "communication_plan": "CP_Basic_ON",
                        },
                        "customer": {"account_id": "100020620"},
                        "network": {
                            "ip_address": None,
                            "fixed_ip_address": None,
                            "ipv6_address": None,
                        },
                        "hardware": {"modem_id": "2221"},
                        "services": {"active": None},
                        "limits": {"data": None, "sms": None},
                        "dates": {"added_at": None, "provisioned_at": None},
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


class SimDetailsIn(BaseModel):
    iccids: list[str] = Field(
        description="ICCID list to enrich with live provider details."
    )
    providers: list[Provider] | None = Field(
        default=None,
        description="Optional provider filter. Resolved SIMs outside it are filtered out.",
    )


class SimDetailsErrorOut(BaseModel):
    code: str
    detail: str | None = None
    retry_after: str | None = None


class SimDetailsItemOut(BaseModel):
    provider: str
    status: Literal["ok", "not_found", "timeout", "error", "rate_limited"]
    data: SubscriptionOut | None = None
    error: SimDetailsErrorOut | None = None


class SimDetailsSummaryOut(BaseModel):
    ok: int = 0
    not_found: int = 0
    timeout: int = 0
    rate_limited: int = 0
    error: int = 0
    total: int = 0


class SimDetailsOut(BaseModel):
    results: dict[str, SimDetailsItemOut]
    summary: SimDetailsSummaryOut
    unresolved: list[str] = Field(default_factory=list)
    filtered_out: list[str] = Field(default_factory=list)


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


class SimSearchCommonFilters(BaseModel):
    iccid: str | None = None
    imsi: str | None = None
    msisdn: str | None = None
    modified_since: datetime | None = None
    modified_till: datetime | None = None
    imei: str | None = None
    operator: str | None = Field(
        default=None,
        description="Case-insensitive substring match on normalized.network.operator.",
    )
    data_service: bool | None = None
    sms_service: bool | None = None
    last_lu_since: datetime | None = None
    last_lu_till: datetime | None = None
    imsi_list: list[str] | None = None
    custom: dict[str, str] = Field(default_factory=dict)


class SimSearchProviderFilters(SimSearchCommonFilters):
    cursor: str | None = None
    limit: int | None = Field(default=None, ge=1, le=500)
    status: str | None = Field(
        default=None,
        description="Provider-native status value for this provider only. Shorthand for one value.",
    )
    statuses: list[str] = Field(
        default_factory=list,
        description="Provider-native status values for this provider only.",
    )


class SimSearchIn(BaseModel):
    cursor: str | None = Field(
        default=None,
        description="Optional global/provider cursor returned by a prior search.",
    )
    limit: int = Field(default=50, ge=1, le=500)
    common: SimSearchCommonFilters = Field(default_factory=SimSearchCommonFilters)
    providers: dict[Provider, SimSearchProviderFilters] = Field(
        default_factory=dict,
        description=(
            "Provider-specific filters. Omit or pass an empty object to query all "
            "providers with the common filters."
        ),
    )


class StatusChangeIn(BaseModel):
    target: str  # Native provider status value.
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


class StatusHistoryRecordOut(BaseModel):
    state: str
    automatic: bool
    time: datetime
    reason: str | None = None
    user: str | None = None

    model_config = {"from_attributes": True}


class StatusHistoryOut(BaseModel):
    iccid: str
    period_start: datetime | None = None
    period_end: datetime | None = None
    records: list[StatusHistoryRecordOut] = Field(
        description="Status history records for this ICCID, newest first when the provider supplies order."
    )


class LocationOut(BaseModel):
    iccid: str
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    accuracy_m: Decimal | None = None
    timestamp: datetime | None = None
    source: str | None = None

    model_config = {"from_attributes": True}


class SimStatsOut(BaseModel):
    total: int
    by_status: dict[str, int] = Field(default_factory=dict)
    by_status_group: dict[str, int] = Field(default_factory=dict)
    stale_lu_count: int = 0
    provider: str | None = None
    fresh_at: datetime
    partial: bool = False
    failed_providers: list[dict[str, str]] = Field(default_factory=list)


class SmsHistoryRecordOut(BaseModel):
    iccid: str
    date: datetime
    message: str
    sms_type: Literal["MO", "MT"] = Field(
        description="SMS direction: 'MO' (mobile-originated) or 'MT' (mobile-terminated)."
    )
    gateway_delivered: bool | None = Field(
        default=None,
        description="Gateway-level delivery confirmation (MT only). None for MO.",
    )
    sms_center_delivered: bool | None = Field(
        default=None,
        description="SMS Center-level delivery confirmation (MT only). None for MO.",
    )

    model_config = {"from_attributes": True}


class SmsHistoryOut(BaseModel):
    iccid: str
    period_start: datetime
    period_end: datetime
    records: list[SmsHistoryRecordOut] = Field(
        description="SMS records for this ICCID, newest first."
    )
