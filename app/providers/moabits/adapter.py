"""Moabits / Orion API 2.0 provider adapter — REST/JSON over HTTPS.

Credentials dict keys:
    base_url (str): API base, e.g. "https://www.api.myorion.co".
    api_key (str): Bearer token/JWT.
    company_codes (list[str]): Company codes owned by this tenant.
    company_id (str): Injected by the service layer — not a stored credential.

Provider-specific fields returned in Subscription.provider_fields (Moabits block):
    iccid, msisdn-side fields kept canonical at the top of Subscription.
    product_id, product_name, product_code, company_code, client_name,
    first_lu, first_cdr, last_lu, last_cdr, firstcdrmonth, last_network,
    imsi_number, services_raw, services,
    number_of_renewals_plan, remaining_renewals_plan,
    plan_start_date, plan_expiration_date, autorenewal,
    data_limit_mb, sms_limit,
    data_service (Enabled|Disabled), sms_service (Enabled|Disabled).

Usage snapshot exposes Moabits-specific raw fields under provider_metrics:
    iccid, active_sim (bool|None), sms_mo (int|None), sms_mt (int|None),
    data_mb (int|None — Moabits returns data in MB).

Orion API 2.0.0 paths used here are documented in the public Swagger:
    GET /api/sim/details/{iccidList}
    GET /api/sim/serviceStatus/{iccidList}
    GET /api/usage/simUsage
    GET /api/sim/connectivityStatus/{iccidList}
    PUT /api/sim/active/
    PUT /api/sim/suspend/
    PUT /api/sim/purge/
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

import httpx

from app.providers.adapter_base import BaseAdapter
from app.providers.base import Provider
from app.shared.errors import (
    ProviderAuthFailed,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderUnavailable,
    ProviderValidationError,
    UnsupportedOperation,
)
from app.config import get_settings
from app.subscriptions.domain import (
    AdministrativeStatus,
    ConnectivityPresence,
    ConnectivityState,
    UsageMetric,
    Subscription,
    SubscriptionSearchFilters,
    UsageSnapshot,
)

from .status_map import map_status


@dataclass(frozen=True)
class _MoabitsCreds:
    base_url: str
    api_key: str
    company_codes: list[str]


def _creds(d: dict[str, Any]) -> _MoabitsCreds:
    codes = d.get("company_codes", [])
    if isinstance(codes, str):
        codes = [codes]
    return _MoabitsCreds(
        base_url=d["base_url"].rstrip("/"),
        api_key=d["api_key"],
        company_codes=codes,
    )


def _client(creds: _MoabitsCreds) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=creds.base_url,
        headers={
            "Authorization": f"Bearer {creds.api_key}",
            "Accept": "application/json",
        },
        timeout=30.0,
    )


def _check(resp: httpx.Response, label: str = "Moabits") -> None:
    if resp.status_code == 401:
        raise ProviderAuthFailed(detail=f"{label} authentication failed")
    if resp.status_code == 403:
        raise ProviderAuthFailed(detail=f"{label} access forbidden")
    if resp.status_code == 429:
        raise ProviderRateLimited(detail=f"{label} rate limit exceeded")
    if resp.status_code >= 500:
        raise ProviderUnavailable(detail=f"{label} HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise ProviderProtocolError(
            detail=f"{label} HTTP {resp.status_code}: {resp.text[:200]}"
        )


async def _get(creds: _MoabitsCreds, path: str, params: dict[str, Any] | None = None) -> Any:
    async with _client(creds) as client:
        try:
            resp = await client.get(path, params=params or {})
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(detail="Moabits timeout") from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailable(detail=f"Moabits network error: {exc}") from exc
    _check(resp)
    return resp.json()


async def _put(creds: _MoabitsCreds, path: str, body: dict[str, Any], idempotency_key: str | None = None) -> Any:
    async with _client(creds) as client:
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            resp = await client.put(path, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(detail="Moabits timeout") from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailable(detail=f"Moabits network error: {exc}") from exc
    _check(resp)
    return resp.json() if resp.content else {}


def _first_from(data: Any, *keys: str) -> Any:
    """Navigate nested dict keys and return the first element of the final list."""
    node: Any = data
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = cast(dict[str, Any], node).get(key)
    if isinstance(node, list) and node:
        return node[0]  # type: ignore[return-value]
    return None


def _normalize_services(raw: Any) -> list[str] | None:
    """Moabits returns `services` as a slash-separated string (e.g. "data/sms").

    Tolerates None, empty string, list (defensive fallback), and trims whitespace.
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        items: list[str] = []
        for x in raw:  # type: ignore[reportUnknownVariableType]
            if x is None:
                continue
            s = str(x).strip().lower()  # type: ignore[arg-type]
            if s:
                items.append(s)
        return items or None
    if isinstance(raw, str):
        if not raw.strip():
            return None
        return [p.strip().lower() for p in raw.split("/") if p.strip()] or None
    return None


def _coerce_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None


def _coerce_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "y", "active", "enabled"):
            return True
        if s in ("false", "0", "no", "n", "inactive", "disabled"):
            return False
    return None


def _build_subscription(
    sim_info: dict[str, Any],
    sim_status_row: dict[str, Any] | None,
    iccid: str,
    company_id: str,
) -> Subscription:
    native_status = (sim_status_row or {}).get("simStatus", "Unknown")

    provider_fields: dict[str, Any] = {}

    # Admin / plan fields from getSimDetails (info.simInfo[]).
    # `firstcdrmonth` and `iccid` are kept under their native key per the
    # provider contract — `iccid` is duplicated into provider_fields for
    # client convenience, alongside the canonical Subscription.iccid.
    for src_key, dst_key in [
        ("iccid", "iccid"),
        ("product_id", "product_id"),
        ("product_name", "product_name"),
        ("product_code", "product_code"),
        ("companyCode", "company_code"),
        ("clientName", "client_name"),
        ("lastNetwork", "last_network"),
        ("imsiNumber", "imsi_number"),
        ("first_lu", "first_lu"),
        ("first_cdr", "first_cdr"),
        ("last_lu", "last_lu"),
        ("last_cdr", "last_cdr"),
        ("firstcdrmonth", "firstcdrmonth"),
        ("imei", "imei"),
        ("autorenewal", "autorenewal"),
        ("dataLimit", "data_limit_mb"),
        ("smsLimit", "sms_limit"),
        ("numberOfRenewalsPlan", "number_of_renewals_plan"),
        ("remainingRenewalsPlan", "remaining_renewals_plan"),
        ("planStartDate", "plan_start_date"),
        ("planExpirationDate", "plan_expiration_date"),
    ]:
        if (v := sim_info.get(src_key)) is not None:
            provider_fields[dst_key] = v

    # `services` is a slash-separated string in Moabits ("data/sms").
    # Preserve the raw value and expose a normalized list.
    if "services" in sim_info:
        services_raw: Any = sim_info.get("services")
        provider_fields["services_raw"] = services_raw
        normalized = _normalize_services(services_raw)
        if normalized is not None:
            provider_fields["services"] = normalized

    # Service flags from getServiceStatus row
    if sim_status_row:
        for svc_key in ("dataService", "smsService"):
            if (v := sim_status_row.get(svc_key)) is not None:
                snake = "data_service" if svc_key == "dataService" else "sms_service"
                provider_fields[snake] = v

    def _parse_dt(v: Any) -> datetime | None:
        if not v:
            return None
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None

    return Subscription(
        iccid=sim_info.get("iccid") or iccid,
        msisdn=sim_info.get("msisdn"),
        imsi=sim_info.get("imsi") or sim_info.get("imsiNumber"),
        status=map_status(native_status),
        native_status=native_status,
        provider=Provider.MOABITS.value,
        company_id=company_id,
        activated_at=_parse_dt(sim_info.get("planStartDate")),
        updated_at=_parse_dt(sim_info.get("last_cdr")),
        provider_fields=provider_fields,
    )


def _usage_metric(metric_type: str, usage: Any, unit: str | None) -> UsageMetric:
    try:
        value = Decimal(str(usage or 0))
    except Exception:
        value = Decimal(0)
    return UsageMetric(metric_type=metric_type, usage=value, unit=unit)


class MoabitsAdapter(BaseAdapter):
    """Moabits / Orion API adapter with circuit breaker (ADR-005).

    Stateless — one singleton in the registry.
    Circuit breaker: opens after 5 failures in 30s window, stays open for 30s.
    """

    def __init__(self):
        super().__init__("moabits")

    async def get_subscription(
        self, iccid: str, credentials: dict[str, Any]
    ) -> Subscription:
        return await self._call_with_breaker(
            self._get_subscription_impl, iccid, credentials
        )

    async def _get_subscription_impl(
        self, iccid: str, credentials: dict[str, Any]
    ) -> Subscription:
        creds = _creds(credentials)
        # getSimDetails and getServiceStatus in parallel — both needed for a full view.
        details_coro = _get(creds, f"/api/sim/details/{iccid}")
        status_coro = _get(creds, f"/api/sim/serviceStatus/{iccid}")
        details_data, status_data = await asyncio.gather(details_coro, status_coro)

        sim_info: dict[str, Any] = _first_from(details_data, "info", "simInfo") or {}
        sim_status_row: dict[str, Any] | None = _first_from(
            status_data, "info", "iccidList"
        )

        return _build_subscription(
            sim_info, sim_status_row, iccid, credentials.get("company_id", "")
        )

    async def get_usage(
        self,
        iccid: str,
        credentials: dict[str, Any],
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        metrics: list[str] | None = None,
    ) -> UsageSnapshot:
        return await self._call_with_breaker(
            self._get_usage_impl, iccid, credentials, start_date, end_date, metrics
        )

    async def _get_usage_impl(
        self,
        iccid: str,
        credentials: dict[str, Any],
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        metrics: list[str] | None = None,
    ) -> UsageSnapshot:
        if metrics:
            raise UnsupportedOperation(
                detail="Moabits usage metrics filtering is not documented"
            )
        creds = _creds(credentials)
        now = datetime.now(tz=timezone.utc)
        start = start_date or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = end_date or now
        if end < start:
            raise ProviderValidationError(detail="end_date must be after start_date")
        fmt = "%Y-%m-%d %H:%M:%S"

        data = await _get(
            creds,
            "/api/usage/simUsage",
            params={
                "iccidList": iccid,
                "initialDate": start.strftime(fmt),
                "finalDate": end.strftime(fmt),
            },
        )
        row: dict[str, Any] = _first_from(data, "info", "simsUsage") or {}
        data_mb = Decimal(str(row.get("data", 0) or 0))
        data_bytes = data_mb * Decimal(1024 * 1024)
        sms_mo = _coerce_int(row.get("smsMO")) or 0
        sms_mt = _coerce_int(row.get("smsMT")) or 0

        # Expose Moabits-specific raw fields under provider_metrics.
        # Moabits' `data` field is reported in MB (per provider analysis).
        provider_metrics: dict[str, Any] = {
            "iccid": row.get("iccid") or iccid,
            "active_sim": _coerce_bool(row.get("activeSim")),
            "sms_mo": _coerce_int(row.get("smsMO")),
            "sms_mt": _coerce_int(row.get("smsMT")),
            "data_mb": _coerce_int(row.get("data")),
        }

        usage_metrics = [
            _usage_metric("data", data_bytes, "bytes"),
            _usage_metric("sms_mo", sms_mo, "count"),
            _usage_metric("sms_mt", sms_mt, "count"),
        ]

        return UsageSnapshot(
            iccid=row.get("iccid") or iccid,
            period_start=start,
            period_end=end,
            data_used_bytes=data_bytes,
            sms_count=sms_mo + sms_mt,
            voice_seconds=0,  # Moabits does not expose voice usage
            provider_metrics=provider_metrics,
            usage_metrics=usage_metrics,
        )

    async def get_presence(
        self, iccid: str, credentials: dict[str, Any]
    ) -> ConnectivityPresence:
        return await self._call_with_breaker(
            self._get_presence_impl, iccid, credentials
        )

    async def _get_presence_impl(
        self, iccid: str, credentials: dict[str, Any]
    ) -> ConnectivityPresence:
        creds = _creds(credentials)
        data = await _get(creds, f"/api/sim/connectivityStatus/{iccid}")
        row: dict[str, Any] = _first_from(data, "info", "connectivityStatus") or {}

        raw_state = str(row.get("status") or "").strip().lower()
        state = (
            ConnectivityState.ONLINE
            if raw_state == "online"
            else (
                ConnectivityState.OFFLINE
                if raw_state == "offline"
                else ConnectivityState.UNKNOWN
            )
        )

        def _str_or_none(v: Any) -> str | None:
            if v is None:
                return None
            s = str(v).strip()
            return s or None

        return ConnectivityPresence(
            iccid=row.get("iccid") or iccid,
            state=state,
            ip_address=None,
            country_code=_str_or_none(row.get("country")),
            rat_type=_str_or_none(row.get("rat")),
            network_name=_str_or_none(row.get("network")),
            last_seen_at=None,
        )

    async def set_administrative_status(
        self,
        iccid: str,
        credentials: dict[str, Any],
        *,
        target: AdministrativeStatus,
        idempotency_key: str,
        data_service: bool | None = None,
        sms_service: bool | None = None,
    ) -> None:
        return await self._call_with_breaker(
            self._set_administrative_status_impl,
            iccid, credentials, target, idempotency_key, data_service, sms_service
        )

    async def _set_administrative_status_impl(
        self,
        iccid: str,
        credentials: dict[str, Any],
        target: AdministrativeStatus,
        idempotency_key: str,
        data_service: bool | None = None,
        sms_service: bool | None = None,
    ) -> None:
        # Guard write-paths with feature flag
        if not get_settings().lifecycle_writes_enabled:
            raise UnsupportedOperation(detail="Lifecycle write operations are disabled by feature flag")

        creds = _creds(credentials)
        # If neither service flag is explicit, enable both (backward compat).
        # Otherwise use the provided values.
        data_enabled = data_service if data_service is not None else True
        sms_enabled = sms_service if sms_service is not None else True
        body: dict[str, Any] = cast(dict[str, Any], {"iccidList": [iccid], "dataService": data_enabled, "smsService": sms_enabled})

        if target == AdministrativeStatus.ACTIVE:
            await _put(creds, "/api/sim/active/", body, idempotency_key=idempotency_key)
        elif target == AdministrativeStatus.SUSPENDED:
            await _put(creds, "/api/sim/suspend/", body, idempotency_key=idempotency_key)
        else:
            raise UnsupportedOperation(
                detail=f"Moabits does not support transitioning to status '{target}'"
            )

    async def purge(
        self, iccid: str, credentials: dict[str, Any], *, idempotency_key: str
    ) -> None:
        return await self._call_with_breaker(
            self._purge_impl, iccid, credentials, idempotency_key
        )

    async def _purge_impl(
        self, iccid: str, credentials: dict[str, Any], idempotency_key: str
    ) -> None:
        if not get_settings().lifecycle_writes_enabled:
            raise UnsupportedOperation(detail="Lifecycle write operations are disabled by feature flag")
        creds = _creds(credentials)
        result = await _put(creds, "/api/sim/purge/", {"iccidList": [iccid]}, idempotency_key=idempotency_key)
        info = result.get("info") if isinstance(result, dict) else None
        if not isinstance(info, dict) or info.get("purged") is not True:
            raise ProviderValidationError(
                detail="Moabits purge did not confirm info.purged=true"
            )

    async def list_subscriptions(
        self,
        credentials: dict[str, Any],
        *,
        cursor: str | None,
        limit: int,
        filters: SubscriptionSearchFilters | None = None,
    ) -> tuple[list[Subscription], str | None]:
        """List subscriptions across the company codes these credentials grant.

        Moabits has no native pagination: `getSimListByCompany` returns all SIMs
        at once. Pagination is applied locally via `cursor` (offset) + `limit`.
        Native rate limits apply at the Moabits endpoint and surface as 429 →
        `ProviderRateLimited`.

        ⚠️  MEMORY WARNING: With 134k+ SIMs, loading all rows into `all_rows`
        dict/list may cause memory pressure. If Moabits introduces server-side
        pagination or cursor support, migrate to that immediately.
        Current workaround: Local pagination accepts requests; consider adding
        a hard limit (e.g., max_offset to cap fetches) if memory becomes an issue.
        """
        if filters and filters.has_filters:
            raise UnsupportedOperation(
                detail="Moabits Search Devices filters are not documented"
            )
        creds = _creds(credentials)
        company_id = credentials.get("company_id", "")
        if not creds.company_codes:
            return [], None

        all_rows: list[dict[str, Any]] = []
        detail_rows_by_iccid: dict[str, dict[str, Any]] = {}
        status_rows_by_iccid: dict[str, dict[str, Any]] = {}
        for company_code in creds.company_codes:
            details_data, status_data = await asyncio.gather(
                _get(creds, f"/api/company/simListDetail/{company_code}"),
                _get(creds, f"/api/company/simList/{company_code}"),
            )

            detail_rows: list[Any] = cast(list[Any], (cast(dict[str, Any], details_data.get("info") or {}).get("simInfo") or []))
            rows: list[Any] = cast(list[Any], (cast(dict[str, Any], status_data.get("info") or {}).get("iccidList") or []))
            if rows:
                all_rows.extend(rows)
            for detail_row in detail_rows:
                if isinstance(detail_row, dict) and detail_row.get("iccid"):  # type: ignore[union-attr]
                    detail_rows_by_iccid[str(detail_row["iccid"])] = cast(dict[str, Any], detail_row)  # type: ignore[arg-type]
            for status_row in rows:
                if isinstance(status_row, dict) and status_row.get("iccid"):  # type: ignore[union-attr]
                    status_rows_by_iccid[str(status_row["iccid"])] = cast(dict[str, Any], status_row)  # type: ignore[arg-type]

        # Local pagination — Moabits returns all SIMs at once.
        try:
            offset = max(int(cursor or "0"), 0)
        except ValueError:
            offset = 0
        page_size = min(max(limit, 1), 500)
        page = all_rows[offset : offset + page_size]

        subs: list[Subscription] = []
        for row in page:
            iccid = row.get("iccid", "")
            detail_row = detail_rows_by_iccid.get(iccid, {})
            status_row = status_rows_by_iccid.get(iccid, row)
            if detail_row:
                subs.append(
                    _build_subscription(detail_row, status_row, iccid, company_id)
                )
            else:
                native_status = row.get("simStatus", "Unknown")
                pf: dict[str, Any] = {}
                if (v := row.get("dataService")) is not None:
                    pf["data_service"] = v
                if (v := row.get("smsService")) is not None:
                    pf["sms_service"] = v

                subs.append(
                    Subscription(
                        iccid=iccid,
                        msisdn=None,
                        imsi=None,
                        status=map_status(native_status),
                        native_status=native_status,
                        provider=Provider.MOABITS.value,
                        company_id=company_id,
                        activated_at=None,
                        updated_at=None,
                        provider_fields=pf,
                    )
                )

        next_cursor = (
            str(offset + len(page))
            if offset + len(page) < len(all_rows)
            else None
        )
        return subs, next_cursor
