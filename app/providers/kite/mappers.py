"""Pure mapping helpers for the Kite provider."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.providers.base import Provider
from app.providers.kite.status_map import map_status
from app.subscriptions.domain import (
    ConnectivityPresence,
    ConnectivityState,
    StatusDetail,
    StatusHistoryRecord,
    Subscription,
    UsageMetric,
    UsageSnapshot,
)


# Presence level semantics mapping:
# - "gprs", "ip", "ip reachability" => ONLINE
# - "gsm" => OFFLINE (camped on 2G voice, no data session)
# - "unknown" or empty => UNKNOWN



def _text(el: ET.Element | None, local: str) -> str | None:
    if el is None:
        return None
    found = el.find(f"{{*}}{local}")
    return found.text if found is not None else None


def _text_any(el: ET.Element | None, *locals_: str) -> str | None:
    for local in locals_:
        value = _text(el, local)
        if value is not None:
            return value
    return None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _coerce_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _coerce_number(value: str | None) -> Decimal | str | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(value)
    except Exception:
        return value


def _child_dict(el: ET.Element | None) -> dict[str, Any] | None:
    if el is None:
        return None
    out: dict[str, Any] = {}
    for child in list(el):
        tag = child.tag.split("}", 1)[-1]
        if list(child):
            nested = _child_dict(child)
            if nested:
                out[tag] = nested
        else:
            value = (child.text or "").strip()
            if value:
                out[tag] = _coerce_bool(value)
                if out[tag] is None:
                    out[tag] = _coerce_number(value)
    return out or None


def _parse_location(el: ET.Element | None) -> dict[str, Any] | None:
    if el is None:
        return None
    coordinates = el.find("{*}coordinates")
    if coordinates is not None:
        latitude = _text(coordinates, "latitude")
        longitude = _text(coordinates, "longitude")
    else:
        latitude = _text(el, "latitude")
        longitude = _text(el, "longitude")
    location = {"lat": latitude, "lng": longitude}
    return location if any(location.values()) else None


def _parse_basic_services(el: ET.Element | None) -> dict[str, Any] | None:
    return _child_dict(el)


def _parse_supplementary_services(el: ET.Element | None) -> list[str] | None:
    if el is None:
        return None
    services: list[str] = []
    for child in list(el):
        tag = child.tag.split("}", 1)[-1]
        value = (child.text or "").strip().lower()
        if value in {"true", "1", "yes"}:
            services.append(tag)
    return services or None


def _parse_consumption_block(el: ET.Element | None) -> dict[str, Any] | None:
    if el is None:
        return None
    block: dict[str, Any] = {}
    for metric in ("voice", "sms", "data"):
        metric_el = el.find(f"{{*}}{metric}")
        if metric_el is None:
            continue
        block[metric] = {
            "limit": _text(metric_el, "limit"),
            "value": _text(metric_el, "value"),
            "thr_reached": _text(metric_el, "thrReached"),
            "traffic_cut": _text(metric_el, "trafficCut"),
            "enabled": _text(metric_el, "enabled"),
        }
    return block or None


def _usage_metrics_from_consumption(
    prefix: str, block: dict[str, Any] | None
) -> list[UsageMetric]:
    if not block:
        return []

    metrics: list[UsageMetric] = []
    unit_map = {"voice": "seconds", "sms": "count", "data": "bytes"}
    for metric_name, metric_data in block.items():
        if not isinstance(metric_data, dict):
            continue
        raw_value = metric_data.get("value")
        try:
            usage_value = Decimal(str(raw_value or 0))
        except Exception:
            usage_value = Decimal(0)
        metrics.append(
            UsageMetric(
                metric_type=f"{prefix}.{metric_name}",
                usage=usage_value,
                unit=unit_map.get(metric_name),
            )
        )
    return metrics


def _parse_nested_block(el: ET.Element | None) -> dict[str, Any] | None:
    if el is None:
        return None
    out: dict[str, Any] = {}
    for child in list(el):
        tag = child.tag.split("}", 1)[-1]
        if list(child):
            nested = _child_dict(child)
            if nested is not None:
                out[tag] = nested
        else:
            out[tag] = (child.text or "").strip()
    return out or None


def parse_subscription(el: ET.Element, iccid: str, company_id: str) -> Subscription:
    native_status = _text(el, "lifeCycleStatus") or "UNKNOWN"
    provider_fields: dict[str, Any] = {}

    # Core subscription identifiers
    provider_fields["subscription_id"] = _text(el, "subscriptionId")

    # Basic SIM information
    field_map = {
        "alias": "alias",
        "simModel": "sim_model",
        "simType": "sim_type",
        "apn": "apn",
        "staticIp": "static_ip",
        "commercialGroup": "commercial_group",
        "supervisionGroup": "supervision_group",
        "billingAccount": "billing_account",
        "orderNumber": "order_number",
        "masterId": "master_id",
        "masterName": "master_name",
        "serviceProviderEnablerId": "service_provider_enabler_id",
        "serviceProviderEnablerName": "service_provider_enabler_name",
        "serviceProviderCommercialId": "service_provider_commercial_id",
        "serviceProviderCommercialName": "service_provider_commercial_name",
        "customerID": "customer_id",
        "customerName": "customer_name",
        "customerCurrency": "customer_currency",
        "endCustomerName": "end_customer_name",
        "endCustomerId": "end_customer_id",
        "sgsnIP": "sgsn_ip",
        "ggsnIP": "ggsn_ip",
        "commModuleManufacturer": "comm_module_manufacturer",
        "commModuleModel": "comm_module_model",
        "billingAccountName": "billing_account_name",
        "servicePack": "service_pack",
        "servicePackId": "service_pack_id",
        "ip": "ip",
        "additionalIp": "additional_ip",
        "eid": "eid",
        "blockReason1": "block_reason_1",
        "blockReason2": "block_reason_2",
        "blockReason3": "block_reason_3",
        "defaultApn": "default_apn",
    }
    for native_key, canonical_key in field_map.items():
        if (value := _text(el, native_key)) is not None:
            provider_fields[canonical_key] = value

    # Date and time fields
    for native_key, canonical_key in (
        ("imei", "imei"),
        ("ratType", "rat_type"),
        ("qci", "qci"),
        ("IMEILastChange", "imei_last_change"),
        ("lastStateChangeDate", "last_state_change_date"),
        ("lastTrafficDate", "last_traffic_date"),
        ("suspensionNextDate", "suspension_next_date"),
        ("provisionDate", "provision_date"),
        ("shippingDate", "shipping_date"),
        ("staticApnIndex", "static_apn_index"),
        ("lteEnabled", "lte_enabled"),
        ("voLteEnabled", "volte_enabled"),
        ("swapStatus", "swap_status"),
        ("subscriptionType", "subscription_type"),
        ("operator", "operator"),
        ("country", "country"),
        ("blockStatus", "block_status"),
        ("profileSwapDate", "profile_swap_date"),
        ("lastBlockedDate", "last_blocked_date"),
        ("lastUnblockedDate", "last_unblocked_date"),
    ):
        value = _text(el, native_key)
        if value is not None:
            provider_fields[canonical_key] = value

    for i in range(1, 5):
        value = _text_any(el, f"customField{i}", f"customField_{i}")
        if value is not None:
            provider_fields[f"custom_field_{i}"] = value

    apn_list = [_text(el, f"apn{i}") for i in range(10) if _text(el, f"apn{i}")]
    if apn_list:
        provider_fields["apn_list"] = apn_list

    static_ips = [
        _text(el, f"staticIpAddress{i}")
        for i in range(10)
        if _text(el, f"staticIpAddress{i}")
    ]
    if static_ips:
        provider_fields["static_ips"] = static_ips

    additional_static_ips = [
        _text(el, f"additionalStaticIpAddress{i}")
        for i in range(10)
        if _text(el, f"additionalStaticIpAddress{i}")
    ]
    if additional_static_ips:
        provider_fields["additional_static_ips"] = additional_static_ips

    for source_name, destination_name in (
        ("gprsStatus", "gprs_status"),
        ("ipStatus", "ip_status"),
    ):
        nested = _parse_nested_block(el.find(f"{{*}}{source_name}"))
        if nested:
            provider_fields[destination_name] = nested

    for source_name, destination_name in (
        ("manualLocation", "manual_location"),
        ("automaticLocation", "automatic_location"),
    ):
        location = _parse_location(el.find(f"{{*}}{source_name}"))
        if location:
            provider_fields[destination_name] = location

    basic_services = _parse_basic_services(el.find("{*}basicServices"))
    if basic_services:
        provider_fields["basic_services"] = basic_services

    supplementary_services = _parse_supplementary_services(el.find("{*}supplServices"))
    if supplementary_services:
        provider_fields["supplementary_services"] = supplementary_services

    for source_name, destination_name in (
        ("consumptionDaily", "consumption_daily"),
        ("consumptionMonthly", "consumption_monthly"),
    ):
        block = _parse_consumption_block(el.find(f"{{*}}{source_name}"))
        if block:
            provider_fields[destination_name] = block

    return Subscription(
        iccid=_text(el, "icc") or iccid,
        msisdn=_text(el, "msisdn"),
        imsi=_text(el, "imsi"),
        status=map_status(native_status),
        native_status=native_status,
        provider=Provider.KITE.value,
        company_id=company_id,
        activated_at=_parse_dt(_text(el, "activationDate")),
        updated_at=_parse_dt(_text_any(el, "lastStateChangeDate", "IMEILastChange")),
        provider_fields=provider_fields,
    )


def parse_usage_snapshot(el: ET.Element, iccid: str) -> UsageSnapshot:
    now = datetime.now(tz=timezone.utc)
    daily = _parse_consumption_block(el.find("{*}consumptionDaily"))
    monthly = _parse_consumption_block(el.find("{*}consumptionMonthly"))

    data_el = el.find("{*}consumptionMonthly/{*}data")
    sms_el = el.find("{*}consumptionMonthly/{*}sms")
    voice_el = el.find("{*}consumptionMonthly/{*}voice")

    def _dec(node: ET.Element | None) -> Decimal:
        value = _text(node, "value") if node is not None else None
        try:
            return Decimal(value or "0")
        except Exception:
            return Decimal(0)

    def _int(node: ET.Element | None) -> int:
        value = _text(node, "value") if node is not None else None
        try:
            return int(float(value or "0"))
        except Exception:
            return 0

    usage_metrics = _usage_metrics_from_consumption("consumption_daily", daily)
    usage_metrics.extend(_usage_metrics_from_consumption("consumption_monthly", monthly))

    return UsageSnapshot(
        iccid=iccid,
        period_start=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
        period_end=now,
        data_used_bytes=_dec(data_el),
        sms_count=_int(sms_el),
        voice_seconds=_int(voice_el),
        provider_metrics={
            "consumption_daily": daily or {},
            "consumption_monthly": monthly or {},
        },
        usage_metrics=usage_metrics,
    )


def parse_presence_fields(el: ET.Element, iccid: str) -> ConnectivityPresence:
    level = (_text(el, "level") or "").strip()
    lvl = level.lower()
    if lvl == "" or lvl == "unknown":
        state = ConnectivityState.UNKNOWN
    elif lvl == "gsm":
        state = ConnectivityState.OFFLINE
    elif lvl in {"gprs", "ip", "ip reachability"}:
        state = ConnectivityState.ONLINE
    else:
        state = ConnectivityState.UNKNOWN

    return ConnectivityPresence(
        iccid=iccid,
        state=state,
        ip_address=_text(el, "ip"),
        country_code=None,
        rat_type=_text_any(el, "ratType"),
        network_name=None,
        last_seen_at=_parse_dt(_text_any(el, "timeStamp", "timestamp")),
    )


def parse_status_detail(el: ET.Element, iccid: str) -> StatusDetail:
    """Parse a status detail response element into a StatusDetail domain object."""
    return StatusDetail(
        iccid=iccid,
        state=_text(el, "state") or "UNKNOWN",
        automatic=_coerce_bool(_text(el, "automatic")) or False,
        current_status_date=_parse_dt(_text(el, "currentStatusDate")) or datetime.now(tz=timezone.utc),
        change_reason=_text(el, "changeReason"),
        user=_text(el, "user"),
    )


def parse_status_history(iccid: str, records: list[ET.Element]) -> list[StatusHistoryRecord]:
    """Parse a list of status history record elements into StatusHistoryRecord domain objects."""
    result: list[StatusHistoryRecord] = []
    for el in records:
        result.append(
            StatusHistoryRecord(
                state=_text(el, "state") or "UNKNOWN",
                automatic=_coerce_bool(_text(el, "automatic")) or False,
                time=_parse_dt(_text(el, "time")) or datetime.now(tz=timezone.utc),
                reason=_text(el, "reason"),
                user=_text(el, "user"),
            )
        )
    return result
