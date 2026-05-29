"""Subscription data normalization utilities."""
import dataclasses
from datetime import UTC, datetime
from typing import Any

from app.subscriptions.domain import Subscription
from app.subscriptions.schemas.sim import SubscriptionOut


def _to_out(sub: Subscription) -> SubscriptionOut:
    data = dataclasses.asdict(sub)
    data["detail_level"] = _detail_level(data)
    data["normalized"] = _normalized_subscription(data)
    return SubscriptionOut(**data)


def _iso_datetime(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _boolish(value: Any) -> bool | Any:
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "enabled"}:
            return True
        if normalized in {"false", "0", "no", "disabled"}:
            return False
    return value


def _first_present(source: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None and value != "":
            return value
    return None


def _custom_fields(provider_fields: dict[str, Any]) -> dict[str, Any]:
    prefixes = (
        "account_custom_",
        "operator_custom_",
        "customer_custom_",
        "custom_field_",
    )
    return {
        key: value
        for key, value in provider_fields.items()
        if any(key.startswith(prefix) for prefix in prefixes)
    }


def _detail_level(data: dict[str, Any]) -> str:
    provider_fields = data.get("provider_fields") or {}
    if "detail_enriched" in provider_fields:
        return "detail" if provider_fields.get("detail_enriched") is True else "summary"
    return "detail"


_STATUS_GROUP_BY_VALUE = {
    "ACTIVE": "active_like",
    "ACTIVATED": "active_like",
    "TEST": "test_like",
    "READY": "test_like",
    "TEST_READY": "test_like",
    "SUSPENDED": "suspended_like",
    "INACTIVE_NEW": "inactive_like",
    "ACTIVATION_READY": "activation_ready_like",
    "ACTIVATION_PENDANT": "activation_pending_like",
    "PENDING": "activation_pending_like",
    "DEACTIVATED": "terminal_like",
    "PURGED": "purged_like",
    "INVENTORY": "inventory_like",
    "REPLACED": "replaced_like",
    "RETIRED": "terminal_like",
    "RESTORE": "restore_like",
    "UNKNOWN": "unknown",
}


def _status_label(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    return cleaned.replace("_", " ").title()


def _status_group(value: Any) -> str:
    if value is None:
        return "unknown"
    normalized = str(value).strip().replace(" ", "_").replace("-", "_").upper()
    if not normalized:
        return "unknown"
    return _STATUS_GROUP_BY_VALUE.get(normalized, "other")


def _status_group_label(group: str) -> str:
    if group == "unknown":
        return "Unknown"
    if group == "other":
        return "Other"
    if group.endswith("_like"):
        return f"{group[:-5].replace('_', ' ').title()}-like"
    return group.replace("_", " ").title()


def _parse_any_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _normalized_subscription(data: dict[str, Any]) -> dict[str, Any]:
    provider_fields: dict[str, Any] = data.get("provider_fields") or {}
    services = provider_fields.get("services")
    if services is None and provider_fields.get("services_raw"):
        services = [
            item.strip().lower()
            for item in str(provider_fields["services_raw"]).split("/")
            if item.strip()
        ]
    status_group = _status_group(data.get("status"))

    return {
        "identity": {
            "imei": _first_present(provider_fields, "imei"),
            "alias": _first_present(provider_fields, "alias"),
            "eid": _first_present(provider_fields, "eid"),
            "euiccid": _first_present(provider_fields, "euiccid"),
            "sim_profile_id": _first_present(provider_fields, "sim_profile_id"),
        },
        "status": {
            "label": _status_label(data.get("status")),
            "group": status_group,
            "group_label": _status_group_label(status_group),
            "source": "provider",
            "last_changed_at": _iso_datetime(
                _first_present(
                    provider_fields, "last_state_change_date", "date_updated"
                )
                or data.get("updated_at")
            ),
        },
        "plan": {
            "name": _first_present(
                provider_fields,
                "product_name",
                "rate_plan",
                "commercial_group",
            ),
            "code": _first_present(provider_fields, "product_code"),
            "id": _first_present(provider_fields, "product_id"),
            "communication_plan": _first_present(provider_fields, "communication_plan"),
            "apn": _first_present(provider_fields, "apn"),
            "apns": _first_present(provider_fields, "apn_list"),
            "started_at": _iso_datetime(
                _first_present(provider_fields, "plan_start_date")
            ),
            "expires_at": _iso_datetime(
                _first_present(provider_fields, "plan_expiration_date")
            ),
        },
        "customer": {
            "name": _first_present(
                provider_fields,
                "client_name",
                "end_customer_name",
                "customer",
            ),
            "id": _first_present(
                provider_fields,
                "end_customer_id",
                "end_consumer_id",
            ),
            "company_code": _first_present(provider_fields, "company_code"),
            "account_id": _first_present(provider_fields, "account_id"),
        },
        "network": {
            "operator": _first_present(provider_fields, "operator"),
            "country": _first_present(provider_fields, "country"),
            "rat_type": _first_present(provider_fields, "rat_type"),
            "last_network": _first_present(provider_fields, "last_network"),
            "ip_address": _first_present(provider_fields, "ip_address"),
            "ipv6_address": _first_present(provider_fields, "ipv6_address"),
            "fixed_ip_address": _first_present(
                provider_fields, "fixed_ip_address", "static_ip"
            ),
            "fixed_ipv6_address": _first_present(provider_fields, "fixed_ipv6_address"),
            "static_ips": _first_present(provider_fields, "static_ips"),
            "additional_static_ips": _first_present(
                provider_fields, "additional_static_ips"
            ),
            "sgsn_ip": _first_present(provider_fields, "sgsn_ip"),
            "ggsn_ip": _first_present(provider_fields, "ggsn_ip"),
            "last_traffic_at": _iso_datetime(
                _first_present(provider_fields, "last_traffic_date")
            ),
            "first_lu_at": _iso_datetime(_first_present(provider_fields, "first_lu")),
            "last_lu_at": _iso_datetime(_first_present(provider_fields, "last_lu")),
            "first_cdr_at": _iso_datetime(_first_present(provider_fields, "first_cdr")),
            "last_cdr_at": _iso_datetime(_first_present(provider_fields, "last_cdr")),
            "gprs_status": _first_present(provider_fields, "gprs_status"),
            "ip_status": _first_present(provider_fields, "ip_status"),
            "location": _first_present(
                provider_fields, "automatic_location", "manual_location"
            ),
        },
        "hardware": {
            "sim_model": _first_present(provider_fields, "sim_model"),
            "module_manufacturer": _first_present(
                provider_fields, "comm_module_manufacturer"
            ),
            "module_model": _first_present(provider_fields, "comm_module_model"),
            "device_id": _first_present(provider_fields, "device_id"),
            "modem_id": _first_present(provider_fields, "modem_id"),
            "imei_last_changed_at": _iso_datetime(
                _first_present(provider_fields, "imei_last_change")
            ),
            "shipped_at": _iso_datetime(
                _first_present(provider_fields, "date_shipped")
            ),
        },
        "services": {
            "active": services,
            "basic": _first_present(provider_fields, "basic_services"),
            "supplementary": _first_present(provider_fields, "supplementary_services"),
            "data_service": _boolish(_first_present(provider_fields, "data_service")),
            "sms_service": _boolish(_first_present(provider_fields, "sms_service")),
        },
        "limits": {
            "data": _first_present(provider_fields, "data_limit_mb"),
            "data_unit": "mb"
            if provider_fields.get("data_limit_mb") is not None
            else None,
            "sms": _first_present(provider_fields, "sms_limit"),
            "daily": _normalize_usage_controls(
                _first_present(provider_fields, "consumption_daily")
            ),
            "monthly": _normalize_usage_controls(
                _first_present(provider_fields, "consumption_monthly")
            ),
        },
        "dates": {
            "added_at": _iso_datetime(_first_present(provider_fields, "date_added")),
            "provisioned_at": _iso_datetime(
                _first_present(provider_fields, "provision_date")
            ),
        },
        "custom_fields": _custom_fields(provider_fields),
    }


def _normalize_usage_controls(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    normalized: dict[str, Any] = {}
    for metric, payload in value.items():
        if not isinstance(payload, dict):
            normalized[metric] = payload
            continue
        normalized[metric] = {
            key: _boolish(payload.get(source_key))
            for key, source_key in (
                ("limit", "limit"),
                ("value", "value"),
                ("threshold_reached", "thr_reached"),
                ("traffic_cut", "traffic_cut"),
                ("enabled", "enabled"),
            )
        }
    return normalized
