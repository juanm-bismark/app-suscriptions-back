"""Subscription filtering and query-parsing utilities."""
import dataclasses
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from app.providers.base import Provider
from app.subscriptions.domain import Subscription, SubscriptionSearchFilters
from app.subscriptions.schemas.sim import SimSearchIn, SimSearchProviderFilters
from app.subscriptions.services.normalization import (
    _boolish,
    _normalized_subscription,
    _parse_any_dt,
)

_REQUEST_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_CUSTOM_FILTER_NORMALIZED_PATHS: dict[str, tuple[str, ...]] = {
    "alias": ("identity", "alias"),
    "imei": ("identity", "imei"),
    "sim_model": ("hardware", "sim_model"),
    "device_id": ("hardware", "device_id"),
    "modem_id": ("hardware", "modem_id"),
    "eid": ("identity", "eid"),
    "euiccid": ("identity", "euiccid"),
    "sim_profile_id": ("identity", "sim_profile_id"),
    "operator": ("network", "operator"),
    "country": ("network", "country"),
    "rat_type": ("network", "rat_type"),
    "ip_address": ("network", "ip_address"),
    "fixed_ip_address": ("network", "fixed_ip_address"),
    "ipv6_address": ("network", "ipv6_address"),
    "last_network": ("network", "last_network"),
    "product_name": ("plan", "name"),
    "rate_plan": ("plan", "name"),
    "commercial_group": ("plan", "name"),
    "communication_plan": ("plan", "communication_plan"),
    "product_code": ("plan", "code"),
    "product_id": ("plan", "id"),
    "service_pack": ("plan", "name"),
    "company_code": ("customer", "company_code"),
    "client_name": ("customer", "name"),
    "customer": ("customer", "name"),
    "account_id": ("customer", "account_id"),
    "end_consumer_id": ("customer", "id"),
    "data_limit_mb": ("limits", "data"),
    "sms_limit": ("limits", "sms"),
}


def _service_matches(value: Any, active: Any, expected: bool) -> bool:
    coerced = _boolish(value)
    if isinstance(coerced, bool):
        return coerced is expected
    if isinstance(active, list):
        lowered = {str(item).strip().lower() for item in active}
        return ("data" in lowered if expected else "data" not in lowered)
    return False


def _sms_service_matches(value: Any, active: Any, expected: bool) -> bool:
    coerced = _boolish(value)
    if isinstance(coerced, bool):
        return coerced is expected
    if isinstance(active, list):
        lowered = {str(item).strip().lower() for item in active}
        return ("sms" in lowered if expected else "sms" not in lowered)
    return False


def _canonical_custom_filter_key(key: str) -> str:
    cleaned = key.strip().replace("-", "_").replace(".", "_")
    compact = cleaned.replace("_", "").lower()
    for prefix in ("customfield", "accountcustom", "operatorcustom", "customercustom"):
        if compact.startswith(prefix):
            suffix = compact[len(prefix):]
            if suffix.isdigit():
                canonical_prefix = {
                    "customfield": "custom_field",
                    "accountcustom": "account_custom",
                    "operatorcustom": "operator_custom",
                    "customercustom": "customer_custom",
                }[prefix]
                return f"{canonical_prefix}_{suffix}"
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", cleaned).lower()
    return re.sub(r"_+", "_", snake).strip("_")


def _normalized_path_value(normalized: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = normalized
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _custom_filter_values(
    provider_fields: dict[str, Any],
    normalized: dict[str, Any],
    key: str,
) -> list[Any]:
    canonical_key = _canonical_custom_filter_key(key)
    values: list[Any] = []
    for candidate_key in (key, canonical_key):
        if candidate_key in provider_fields:
            values.append(provider_fields[candidate_key])
    path = _CUSTOM_FILTER_NORMALIZED_PATHS.get(canonical_key)
    if path is not None:
        values.append(_normalized_path_value(normalized, path))
    custom_fields = normalized.get("custom_fields") if isinstance(normalized, dict) else None
    if isinstance(custom_fields, dict):
        for candidate_key in (key, canonical_key):
            if candidate_key in custom_fields:
                values.append(custom_fields[candidate_key])
    return values


def _custom_filter_matches_value(value: Any, expected: str) -> bool:
    expected_text = expected.strip()
    if not expected_text:
        return True
    expected_bool = _boolish(expected_text)
    actual_bool = _boolish(value)
    if isinstance(expected_bool, bool) and isinstance(actual_bool, bool):
        return actual_bool is expected_bool
    if isinstance(value, list):
        return any(_custom_filter_matches_value(item, expected_text) for item in value)
    return expected_text.lower() in str(value or "").strip().lower()


def _custom_filters_match(
    provider_fields: dict[str, Any],
    normalized: dict[str, Any],
    filters: dict[str, str],
) -> bool:
    for key, expected in filters.items():
        if not expected.strip():
            continue
        values = _custom_filter_values(provider_fields, normalized, key)
        if not values or not any(_custom_filter_matches_value(value, expected) for value in values):
            return False
    return True


def _requires_post_filter(filters: SubscriptionSearchFilters | None) -> bool:
    if filters is None:
        return False
    return any(
        (
            bool(filters.imei),
            bool(filters.operator),
            filters.data_service is not None,
            filters.sms_service is not None,
            filters.last_lu_since is not None,
            filters.last_lu_till is not None,
            bool(filters.imsi_list),
            bool(filters.custom),
        )
    )


def _post_filter_matches(sub: Subscription, filters: SubscriptionSearchFilters) -> bool:
    data = dataclasses.asdict(sub)
    normalized = _normalized_subscription(data)
    identity = normalized.get("identity") or {}
    network = normalized.get("network") or {}
    services = normalized.get("services") or {}
    provider_fields = data.get("provider_fields") or {}

    if filters.imei and str(identity.get("imei") or "").strip() != filters.imei.strip():
        return False
    if filters.operator:
        haystack = str(network.get("operator") or "").lower()
        if filters.operator.strip().lower() not in haystack:
            return False
    if filters.imsi_list:
        allowed = {str(value).strip() for value in filters.imsi_list if str(value).strip()}
        if str(sub.imsi or "").strip() not in allowed:
            return False
    active_services = services.get("active")
    if filters.data_service is not None and not _service_matches(
        services.get("data_service"), active_services, filters.data_service
    ):
        return False
    if filters.sms_service is not None and not _sms_service_matches(
        services.get("sms_service"), active_services, filters.sms_service
    ):
        return False
    last_lu = _parse_any_dt(network.get("last_lu_at"))
    if filters.last_lu_since is not None:
        since = _parse_any_dt(filters.last_lu_since)
        if last_lu is None or since is None or last_lu < since:
            return False
    if filters.last_lu_till is not None:
        till = _parse_any_dt(filters.last_lu_till)
        if till is None:
            return False
        if last_lu is not None and last_lu > till:
            return False
    if filters.custom and not _custom_filters_match(provider_fields, normalized, filters.custom):
        return False
    return True


def _apply_post_filters(
    subs: list[Subscription],
    filters: SubscriptionSearchFilters | None,
) -> list[Subscription]:
    if not _requires_post_filter(filters):
        return subs
    assert filters is not None
    return [sub for sub in subs if _post_filter_matches(sub, filters)]


def _parse_metric_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    metrics = [item.strip() for item in raw.split(",") if item.strip()]
    return metrics or None


def _parse_custom_filters(raw: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in raw or []:
        separator = "=" if "=" in item else ":"
        if separator not in item:
            raise HTTPException(
                status_code=400,
                detail="custom filters must use key=value or key:value format",
            )
        key, value = item.split(separator, 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise HTTPException(
                status_code=400,
                detail="custom filters must include non-empty key and value",
            )
        parsed[key] = value
    return parsed


def _parse_query_dt(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    if not _REQUEST_DT_RE.fullmatch(value):
        raise HTTPException(
            status_code=400,
            detail=f"{field} must use yyyy-MM-ddTHH:mm:ssZ format",
        )
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _parse_optional_bool_query(value: str | None, field: str) -> bool | None:
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise HTTPException(
        status_code=400,
        detail=f"{field} must be true or false",
    )


def _build_filters(
    *,
    status_filter: str | None,
    modified_since: str | None,
    modified_till: str | None,
    iccid: str | None,
    imsi: str | None,
    msisdn: str | None,
    custom: list[str] | None,
) -> SubscriptionSearchFilters:
    return SubscriptionSearchFilters(
        status=status_filter or None,
        modified_since=_parse_query_dt(modified_since, "modified_since"),
        modified_till=_parse_query_dt(modified_till, "modified_till"),
        iccid=iccid,
        imsi=imsi,
        msisdn=msisdn,
        custom=_parse_custom_filters(custom),
    )


def _bootstrap_filters_for_provider(provider: str) -> SubscriptionSearchFilters:
    if provider == Provider.TELE2.value:
        return SubscriptionSearchFilters(
            modified_since=datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)
        )
    return SubscriptionSearchFilters()


def _merge_search_filters(
    body: SimSearchIn,
    provider_filters: SimSearchProviderFilters,
    status: str | None,
) -> SubscriptionSearchFilters:
    common = body.common
    return SubscriptionSearchFilters(
        status=status,
        modified_since=provider_filters.modified_since or common.modified_since,
        modified_till=provider_filters.modified_till or common.modified_till,
        iccid=provider_filters.iccid or common.iccid,
        imsi=provider_filters.imsi or common.imsi,
        msisdn=provider_filters.msisdn or common.msisdn,
        imei=provider_filters.imei or getattr(common, "imei", None),
        operator=provider_filters.operator or getattr(common, "operator", None),
        data_service=provider_filters.data_service
        if provider_filters.data_service is not None
        else getattr(common, "data_service", None),
        sms_service=provider_filters.sms_service
        if provider_filters.sms_service is not None
        else getattr(common, "sms_service", None),
        last_lu_since=provider_filters.last_lu_since
        or getattr(common, "last_lu_since", None),
        last_lu_till=provider_filters.last_lu_till
        or getattr(common, "last_lu_till", None),
        imsi_list=provider_filters.imsi_list or getattr(common, "imsi_list", None),
        custom={**common.custom, **provider_filters.custom},
    )


def _search_status_values(
    provider_filters: SimSearchProviderFilters,
) -> list[str | None]:
    statuses: list[str | None] = []
    if provider_filters.status:
        statuses.append(provider_filters.status)
    statuses.extend(status for status in provider_filters.statuses if status)
    deduped = list(dict.fromkeys(statuses))
    return deduped or [None]
