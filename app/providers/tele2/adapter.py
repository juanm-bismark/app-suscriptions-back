"""Tele2 provider adapter — REST/JSON over HTTPS.

Credentials dict keys:
    base_url (str): API base, e.g. "https://api.tele2.com".
    username/api_key (str): Cisco Control Center HTTP Basic auth.
    account_id (str, optional): Account ID; defaults to the caller's account.
    api_version (str, optional): Cisco API version prefix, default "v1".
        Only v1/1 is supported; other values raise 10000024 Invalid apiVersion.
    company_id (str): Injected by the service layer — not a stored credential.

Provider-specific fields returned in Subscription.provider_fields (Tele2 block):
    rate_plan, communication_plan, overage_limit_override,
    test_ready_data_limit, test_ready_sms_limit,
    test_ready_voice_limit, test_ready_csd_limit,
    device_id, modem_id, date_shipped,
    account_custom_1..10, operator_custom_1..5, customer_custom_1..5,
    p5g_commercial_status, ip_address, date_session_started, last_session_end_time.
"""

import asyncio
import base64
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from urllib.parse import urlparse

import httpx

from app.config import get_settings
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
from app.subscriptions.domain import (
    AdministrativeStatus,
    ConnectivityPresence,
    ConnectivityState,
    Subscription,
    SubscriptionSearchFilters,
    UsageMetric,
    UsageSnapshot,
)

from .status_map import map_status, to_native

_DEFAULT_COBRAND_HOST = "restapi3.jasper.com"
_API_VERSION = "1"
_REQUEST_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DEFAULT_MAX_TPS = 1.0
_MAX_CONFIGURABLE_TPS = 1000.0
_DETAIL_ENRICHMENT_LIMIT = 5


@dataclass(frozen=True)
class _Tele2Creds:
    base_url: str
    api_key: str
    username: str
    account_id: str | None


class _Tele2RateLimiter:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_started: dict[str, float] = {}
        self._backoff_until: dict[str, float] = {}
        self._backoff_delay: dict[str, float] = {}
        self._guard = asyncio.Lock()

    async def call(
        self,
        key: str,
        max_tps: float,
        fn: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        lock = await self._lock_for(key)
        async with lock:
            now = time.monotonic()
            backoff_until = self._backoff_until.get(key, 0.0)
            wait_for_backoff = max(0.0, backoff_until - now)
            if wait_for_backoff:
                await asyncio.sleep(wait_for_backoff)

            interval = 1.0 / max(max_tps, 0.001)
            last_started = self._last_started.get(key, 0.0)
            wait_for_tps = max(0.0, (last_started + interval) - time.monotonic())
            if wait_for_tps:
                await asyncio.sleep(wait_for_tps)

            self._last_started[key] = time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except ProviderRateLimited:
                self._record_rate_limited(key)
                raise
            else:
                self._record_success(key)
                return result

    async def _lock_for(self, key: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            return lock

    def _record_rate_limited(self, key: str) -> None:
        next_delay = min(self._backoff_delay.get(key, 0.0) + 1.0, 30.0)
        self._backoff_delay[key] = next_delay
        self._backoff_until[key] = time.monotonic() + next_delay

    def _record_success(self, key: str) -> None:
        current = self._backoff_delay.get(key, 0.0)
        if current <= 1.0:
            self._backoff_delay.pop(key, None)
            self._backoff_until.pop(key, None)
        else:
            self._backoff_delay[key] = current - 1.0


def _creds(d: dict[str, Any]) -> _Tele2Creds:
    username = d.get("username")
    if not username:
        raise ProviderAuthFailed(
            detail="Tele2 Cisco Control Center REST credentials require username for HTTP Basic auth"
        )
    base_url = d.get("base_url") or d.get("cobrand_url") or _DEFAULT_COBRAND_HOST
    raw_base_url = str(base_url).strip().rstrip("/")
    if not raw_base_url.startswith(("http://", "https://")):
        raw_base_url = f"https://{raw_base_url}"
    parsed_base_url = urlparse(raw_base_url)
    if not parsed_base_url.netloc:
        raise ProviderAuthFailed(
            detail="Tele2 Cisco Control Center REST credentials require a valid cobrand_url"
        )
    requested_api_version = str(d.get("api_version", _API_VERSION)).lstrip("v")
    if requested_api_version != _API_VERSION:
        raise ProviderValidationError(detail="10000024 Invalid apiVersion")
    return _Tele2Creds(
        base_url=f"{parsed_base_url.scheme}://{parsed_base_url.netloc}",
        username=username,
        api_key=d["api_key"],
        account_id=d.get("account_id"),  # type: ignore[assignment]
    )


def _rate_limit_key(credentials: dict[str, Any], creds: _Tele2Creds) -> str:
    company_id = str(credentials.get("company_id") or "")
    account_id = str(creds.account_id or "")
    if company_id or account_id:
        return f"{creds.base_url}|{company_id}|{account_id}"
    username = str(credentials.get("username") or "")
    return f"{creds.base_url}|{username}"


def _max_tps(credentials: dict[str, Any]) -> float:
    raw = credentials.get("max_tps")
    if raw is None and isinstance(credentials.get("account_scope"), dict):
        raw = credentials["account_scope"].get("max_tps")
    if raw is None:
        return _DEFAULT_MAX_TPS
    try:
        value = float(raw)
    except TypeError, ValueError:
        return _DEFAULT_MAX_TPS
    return min(max(value, _DEFAULT_MAX_TPS), _MAX_CONFIGURABLE_TPS)


def _client(creds: _Tele2Creds) -> httpx.AsyncClient:
    headers = {"Accept": "application/json"}
    token = f"{creds.username}:{creds.api_key}".encode()
    b64 = base64.b64encode(token).decode("ascii")
    headers["Authorization"] = f"Basic {b64}"
    return httpx.AsyncClient(
        base_url=creds.base_url,
        headers=headers,
        timeout=max(float(get_settings().tele2_request_timeout_seconds), 0.1),
    )


def _path(creds: _Tele2Creds, suffix: str) -> str:
    # Ensure suffix begins with '/'
    if not suffix.startswith("/"):
        suffix = "/" + suffix
    return f"/rws/api/v{_API_VERSION}{suffix}"


def _check(resp: httpx.Response, label: str = "Tele2") -> None:
    provider_error_code = None
    provider_error_message = None
    error_payload = None
    if resp.status_code >= 400:
        try:
            error_payload = resp.json()
        except Exception:
            error_payload = None
    if isinstance(error_payload, dict):
        provider_error_code = error_payload.get("errorCode")
        provider_error_message = error_payload.get("errorMessage")
    error_detail = provider_error_message or resp.text[:200]

    if provider_error_code == "40000029":
        raise ProviderRateLimited(
            detail=provider_error_message or "Tele2 rate limit exceeded",
            retry_after=resp.headers.get("Retry-After"),
            extra={
                "provider_error_code": provider_error_code,
                "provider_error_message": provider_error_message,
            },
        )
    if resp.status_code == 401:
        raise ProviderAuthFailed(
            detail=f"{label} authentication failed",
            extra={
                "provider_error_code": provider_error_code,
                "provider_error_message": provider_error_message,
            },
        )
    if resp.status_code == 403:
        raise ProviderAuthFailed(
            detail=f"{label} access forbidden",
            extra={
                "provider_error_code": provider_error_code,
                "provider_error_message": provider_error_message,
            },
        )
    if resp.status_code == 429:
        raise ProviderRateLimited(
            detail=f"{label} rate limit exceeded",
            retry_after=resp.headers.get("Retry-After"),
            extra={
                "provider_error_code": provider_error_code,
                "provider_error_message": provider_error_message,
            },
        )
    if resp.status_code >= 500:
        raise ProviderUnavailable(
            detail=f"{label} HTTP {resp.status_code}: {error_detail}",
            extra={
                "provider_error_code": provider_error_code,
                "provider_error_message": provider_error_message,
            },
        )
    if resp.status_code >= 400:
        if resp.status_code == 404:
            raise ProviderProtocolError(
                detail=f"{label} HTTP {resp.status_code}: {error_detail}",
                extra={
                    "provider_error_code": provider_error_code,
                    "provider_error_message": provider_error_message,
                },
            )
        raise ProviderValidationError(
            detail=f"{label} HTTP {resp.status_code}: {error_detail}",
            provider_error_code=provider_error_code,
            provider_error_message=provider_error_message,
        )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_request_dt(value: str) -> datetime:
    if not _REQUEST_DT_RE.fullmatch(value):
        raise ValueError(value)
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _format_request_dt(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_usage_date(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%d")


def _is_http_not_found(exc: ProviderProtocolError) -> bool:
    detail = exc.detail or ""
    return "HTTP 404" in detail


async def _get(
    creds: _Tele2Creds, path: str, params: dict[str, Any] | None = None
) -> Any:
    async with _client(creds) as client:
        try:
            resp = await client.get(_path(creds, path), params=params or {})
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(detail="Tele2 timeout") from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailable(detail=f"Tele2 network error: {exc}") from exc
    _check(resp)
    return await _response_json(resp, "Tele2")


async def _put(
    creds: _Tele2Creds,
    path: str,
    body: dict[str, Any],
    idempotency_key: str | None = None,
) -> Any:
    async with _client(creds) as client:
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            resp = await client.put(_path(creds, path), json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(detail="Tele2 timeout") from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailable(detail=f"Tele2 network error: {exc}") from exc
    _check(resp)
    return await _response_json(resp, "Tele2") if resp.content else {}


async def _response_json(resp: httpx.Response, label: str) -> Any:
    try:
        return await asyncio.to_thread(resp.json)
    except ValueError as exc:
        raise ProviderProtocolError(
            detail=f"{label} returned non-JSON response"
        ) from exc


def _parse_subscription(data: dict[str, Any], company_id: str) -> Subscription:
    native_status = cast(str, data.get("status", "UNKNOWN"))  # type: ignore[arg-type]

    # Extract Tele2-specific fields into provider_fields
    provider_fields: dict[str, Any] = {}
    field_map = {
        "ratePlan": "rate_plan",
        "communicationPlan": "communication_plan",
        "overageLimitOverride": "overage_limit_override",
        "testReadyDataLimit": "test_ready_data_limit",
        "testReadySmsLimit": "test_ready_sms_limit",
        "testReadyVoiceLimit": "test_ready_voice_limit",
        "testReadyCsdLimit": "test_ready_csd_limit",
    }
    for native_key, canonical_key in field_map.items():
        if (v := data.get(native_key)) is not None:  # type: ignore[union-attr]
            provider_fields[canonical_key] = v

    extra_fields = {
        "imei": "imei",
        "customer": "customer",
        "endConsumerId": "end_consumer_id",
        "accountId": "account_id",
        "fixedIPAddress": "fixed_ip_address",
        "fixedIpv6Address": "fixed_ipv6_address",
        "ipv6Address": "ipv6_address",
        "deviceID": "device_id",
        "modemID": "modem_id",
        "eid": "eid",
        "euiccid": "euiccid",
        "simProfileId": "sim_profile_id",
        "simNotes": "sim_notes",
        "mec": "mec",
        "detail_enriched": "detail_enriched",
        "dateAdded": "date_added",
        "dateUpdated": "date_updated",
        "dateShipped": "date_shipped",
        "p5gCommercialStatus": "p5g_commercial_status",
        "ipAddress": "ip_address",
        "dateSessionStarted": "date_session_started",
        "lastSessionEndTime": "last_session_end_time",
    }
    for native_key, canonical_key in extra_fields.items():
        if (v := data.get(native_key)) is not None:  # type: ignore[union-attr]
            provider_fields[canonical_key] = v

    for i in range(1, 11):
        key = f"accountCustom{i}"
        if (v := data.get(key)) is not None:  # type: ignore[union-attr]
            provider_fields[f"account_custom_{i}"] = v
    for i in range(1, 6):
        for native_prefix, canonical_prefix in (
            ("operatorCustom", "operator_custom"),
            ("customerCustom", "customer_custom"),
        ):
            key = f"{native_prefix}{i}"
            if (v := data.get(key)) is not None:  # type: ignore[union-attr]
                provider_fields[f"{canonical_prefix}_{i}"] = v

    return Subscription(
        iccid=cast(str, data["iccid"]),  # type: ignore[arg-type]
        msisdn=cast(str | None, data.get("msisdn")),  # type: ignore[arg-type]
        imsi=cast(str | None, data.get("imsi")),  # type: ignore[arg-type]
        status=map_status(native_status),
        native_status=native_status,
        provider=Provider.TELE2.value,
        company_id=company_id,
        activated_at=_parse_dt(cast(str | None, data.get("dateActivated"))),  # type: ignore[arg-type]
        updated_at=_parse_dt(
            cast(str | None, data.get("dateModified") or data.get("dateUpdated"))
        ),  # type: ignore[arg-type]
        provider_fields=provider_fields,
    )


def _metric_unit(metric_type: str) -> str | None:
    if metric_type == "data":
        return "bytes"
    if metric_type in {"voice", "vmo", "vmt"}:
        return "seconds"
    if metric_type in {"sms", "smo", "smt"}:
        return "count"
    if metric_type == "csd":
        return "bytes"
    return None


class Tele2Adapter(BaseAdapter):
    """Tele2 REST adapter with circuit breaker (ADR-005).

    Stateless — one singleton in the registry.
    Circuit breaker: opens after 5 failures in 30s window, stays open for 30s.
    """

    def __init__(self) -> None:
        super().__init__("tele2")
        self._rate_limiter = _Tele2RateLimiter()

    async def _limited_get(
        self,
        credentials: dict[str, Any],
        creds: _Tele2Creds,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return await self._rate_limiter.call(
            _rate_limit_key(credentials, creds),
            _max_tps(credentials),
            _get,
            creds,
            path,
            params,
        )

    async def _limited_put(
        self,
        credentials: dict[str, Any],
        creds: _Tele2Creds,
        path: str,
        body: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> Any:
        return await self._rate_limiter.call(
            _rate_limit_key(credentials, creds),
            _max_tps(credentials),
            _put,
            creds,
            path,
            body,
            idempotency_key,
        )

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
        data = await self._limited_get(credentials, creds, f"/devices/{iccid}")
        return _parse_subscription(data, credentials.get("company_id", ""))

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
        creds = _creds(credentials)
        params: dict[str, Any] = {}
        if start_date is not None or end_date is not None:
            if start_date is None or end_date is None:
                raise ProviderValidationError(
                    detail="Tele2 usage date range requires both start_date and end_date"
                )
            if end_date < start_date:
                raise ProviderValidationError(
                    detail="end_date must be after start_date"
                )
            if end_date - start_date > timedelta(days=30):
                raise ProviderValidationError(
                    detail="Tele2 Get Device Usage range cannot exceed 30 days"
                )
            params["startDate"] = _format_usage_date(start_date)
            params["endDate"] = _format_usage_date(end_date)
        if metrics:
            params["metrics"] = ",".join(metrics)

        data = await self._limited_get(
            credentials,
            creds,
            f"/devices/{iccid}/usage",
            params=params,
        )

        # Supports both object-based and metric-list response shapes.
        metric_map: dict[str, dict[str, Any]] = {}
        metrics_list = cast(
            list[Any] | None, data.get("metrics") if isinstance(data, dict) else None
        )  # type: ignore[arg-type]
        if isinstance(metrics_list, list):
            for item in metrics_list:  # type: ignore[assignment]
                if not isinstance(item, dict):
                    continue
                mtype = str(cast(Any, item.get("metricType")) or "").lower()  # type: ignore[arg-type]
                if mtype:
                    metric_map[mtype] = {
                        "usage": item.get("usage", 0),  # type: ignore[union-attr]
                        "unit": item.get("unit"),  # type: ignore[union-attr]
                    }

        voice = cast(dict[str, Any], data.get("voice") or {})  # type: ignore[arg-type]
        sms = cast(dict[str, Any], data.get("sms") or {})  # type: ignore[arg-type]
        data_block = cast(dict[str, Any], data.get("data") or {})  # type: ignore[arg-type]

        def _num(v: Any) -> Decimal:
            try:
                return Decimal(str(v or 0))
            except Exception:
                return Decimal(0)

        if metric_map:
            vmo = _num(metric_map.get("vmo", {}).get("usage"))
            vmt = _num(metric_map.get("vmt", {}).get("usage"))
            smo = _num(metric_map.get("smo", {}).get("usage"))
            data_used = _num(metric_map.get("data", {}).get("usage"))
            smt = _num(metric_map.get("smt", {}).get("usage"))
            voice_seconds = int(vmo + vmt)
            sms_count = int(smo + smt)
        else:
            data_used = _num(data_block.get("used", 0))  # type: ignore[union-attr]
            voice_seconds = int(_num(voice.get("used", 0)))  # type: ignore[union-attr]
            sms_count = int(_num(sms.get("sent", 0) or sms.get("used", 0)))  # type: ignore[union-attr]

        now = datetime.now(tz=UTC)
        if metric_map:
            usage_metrics = [
                UsageMetric(
                    metric_type=metric_type,
                    usage=_num(payload.get("usage")),
                    unit=_metric_unit(metric_type),
                )
                for metric_type, payload in metric_map.items()
            ]
        else:
            usage_metrics = [
                UsageMetric(metric_type="data", usage=data_used, unit="bytes"),
                UsageMetric(
                    metric_type="voice",
                    usage=_num(voice.get("used", 0)),
                    unit="seconds",
                ),
                UsageMetric(
                    metric_type="sms",
                    usage=_num(sms.get("sent", 0) or sms.get("used", 0)),
                    unit="count",
                ),
            ]
        return UsageSnapshot(
            iccid=iccid,
            period_start=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
            period_end=now,
            data_used_bytes=data_used,
            sms_count=sms_count,
            voice_seconds=voice_seconds,
            provider_metrics=metric_map,
            usage_metrics=usage_metrics,
        )

    async def _get_session_payload(
        self, credentials: dict[str, Any], creds: _Tele2Creds, iccid: str
    ) -> dict[str, Any] | None:
        # Use single canonical path for session details per implementation plan
        try:
            data = await self._limited_get(
                credentials,
                creds,
                f"/devices/{iccid}/sessionDetails",
            )
            if isinstance(data, dict):
                return cast(dict[str, Any], data)  # type: ignore[return-value]
        except ProviderProtocolError as exc:
            if _is_http_not_found(exc):
                return None
            raise
        return None

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
        session = await self._get_session_payload(credentials, creds, iccid)
        ip_address = None
        last_seen_at = None
        state = ConnectivityState.UNKNOWN

        if session:
            ip_address = cast(
                str | None,
                (
                    cast(dict[str, Any], session).get("ipAddress")  # type: ignore[arg-type]
                    or (cast(dict[str, Any], session).get("session") or {}).get(
                        "ipAddress"
                    )  # type: ignore[arg-type]
                ),
            )
            started = cast(
                str | None,
                (
                    cast(dict[str, Any], session).get("dateSessionStarted")  # type: ignore[arg-type]
                    or (cast(dict[str, Any], session).get("session") or {}).get(
                        "dateSessionStarted"
                    )  # type: ignore[arg-type]
                ),
            )
            ended = cast(
                str | None,
                (
                    cast(dict[str, Any], session).get("lastSessionEndTime")  # type: ignore[arg-type]
                    or (cast(dict[str, Any], session).get("session") or {}).get(
                        "lastSessionEndTime"
                    )  # type: ignore[arg-type]
                ),
            )
            if ip_address and not ended:
                state = ConnectivityState.ONLINE
                last_seen_at = _parse_dt(started)
            elif ended:
                state = ConnectivityState.OFFLINE
                last_seen_at = _parse_dt(ended)

        return ConnectivityPresence(
            iccid=iccid,
            state=state,
            ip_address=ip_address,
            country_code=None,
            rat_type=None,
            network_name=None,
            last_seen_at=last_seen_at,
        )

    async def set_administrative_status(
        self,
        iccid: str,
        credentials: dict[str, Any],
        *,
        target: AdministrativeStatus,
        idempotency_key: str,
        **_: Any,
    ) -> None:
        return await self._call_with_breaker(
            self._set_administrative_status_impl,
            iccid,
            credentials,
            target,
            idempotency_key,
        )

    async def _set_administrative_status_impl(
        self,
        iccid: str,
        credentials: dict[str, Any],
        target: AdministrativeStatus,
        idempotency_key: str,
    ) -> None:
        # Feature flag: guard write-paths
        if not get_settings().lifecycle_writes_enabled:
            raise UnsupportedOperation(
                detail="Lifecycle write operations are disabled by feature flag"
            )

        native = to_native(target)
        if native is None:
            raise UnsupportedOperation(
                detail=f"Tele2 does not support transitioning to status '{target}'"
            )
        creds = _creds(credentials)
        await self._limited_put(
            credentials,
            creds,
            f"/devices/{iccid}",
            {"status": native},
            idempotency_key=idempotency_key,
        )

    async def purge(
        self, iccid: str, credentials: dict[str, Any], *, idempotency_key: str
    ) -> None:
        """Purge a device by transitioning it to PURGED status.

        This is an alias for set_administrative_status(..., target=AdministrativeStatus.PURGED).
        Both routes reach the same provider state — this method is included to match the
        SubscriptionProvider protocol interface.
        """
        return await self._call_with_breaker(
            self._purge_impl, iccid, credentials, idempotency_key
        )

    async def _purge_impl(
        self, iccid: str, credentials: dict[str, Any], idempotency_key: str
    ) -> None:
        await self._set_administrative_status_impl(
            iccid,
            credentials,
            target=AdministrativeStatus.PURGED,
            idempotency_key=idempotency_key,
        )

    def supports_list_filter(self, filter_name: str) -> bool:
        return filter_name == "iccid"

    def bootstrap_filters(self) -> SubscriptionSearchFilters:
        return SubscriptionSearchFilters(
            modified_since=datetime.now(UTC).replace(microsecond=0)
            - timedelta(days=365)
            + timedelta(seconds=1)
        )

    async def list_subscriptions(
        self,
        credentials: dict[str, Any],
        *,
        cursor: str | None,
        limit: int,
        filters: SubscriptionSearchFilters | None = None,
    ) -> tuple[list[Subscription], str | None]:
        return await self._call_with_breaker(
            self._list_subscriptions_impl,
            credentials,
            cursor=cursor,
            limit=limit,
            filters=filters,
        )

    async def _list_subscriptions_impl(
        self,
        credentials: dict[str, Any],
        *,
        cursor: str | None,
        limit: int,
        filters: SubscriptionSearchFilters | None = None,
    ) -> tuple[list[Subscription], str | None]:
        """List devices on the Tele2 account these credentials authenticate to.

        Native pagination: `pageNumber` (1-based) + `pageSize` (1..50).
        Native rate limits apply at the Tele2 endpoint and surface as 429 →
        `ProviderRateLimited`.
        """
        creds = _creds(credentials)
        company_id = credentials.get("company_id", "")
        # cursor may be a simple page number or a composite string like
        # "page:1|since:2026-01-01T00:00:00Z|till:2027-01-01T00:00:00Z"
        page = 1
        since = None
        till = None
        if cursor:
            parts = cursor.split("|")
            for part in parts:
                if part.startswith("page:"):
                    try:
                        page = max(int(part.split(":", 1)[1]), 1)
                    except Exception:
                        page = 1
                if part.startswith("since:"):
                    since = part.split(":", 1)[1]
                if part.startswith("till:"):
                    till = part.split(":", 1)[1]

        now = datetime.now(tz=UTC).replace(microsecond=0)
        if filters and filters.modified_since is not None:
            since_dt = filters.modified_since.astimezone(UTC).replace(microsecond=0)
            since = _format_request_dt(since_dt)
        if not since:
            raise ProviderValidationError(detail="10000003 ModifiedSince is required")
        try:
            since_dt = _parse_request_dt(since)
        except ValueError as exc:
            raise ProviderValidationError(
                detail=f"10000003 ModifiedSince must use yyyy-MM-ddTHH:mm:ssZ format: {since}"
            ) from exc
        if since_dt > now:
            raise ProviderValidationError(
                detail="10002119 ModifiedSince cannot be a future date"
            )
        if now - since_dt > timedelta(days=365):
            raise ProviderValidationError(
                detail="10000045 ModifiedSince cannot be more than one year old"
            )

        if till:
            try:
                till_dt = _parse_request_dt(till)
            except ValueError as exc:
                raise ProviderValidationError(
                    detail=f"Invalid Tele2 modifiedTill cursor value: {till}"
                ) from exc
        else:
            till_dt = since_dt + timedelta(days=365)
            till = _format_request_dt(till_dt)
        if filters and filters.modified_till is not None:
            till_dt = filters.modified_till.astimezone(UTC).replace(microsecond=0)
            till = _format_request_dt(till_dt)
        if till_dt is not None and till_dt - since_dt > timedelta(days=365):
            raise ProviderValidationError(
                detail="Tele2 Search Devices window cannot exceed one year"
            )

        params: dict[str, Any] = {
            "pageSize": min(max(limit, 1), 50),
            "pageNumber": page,
            "modifiedSince": since,
            "modifiedTill": till,
        }
        if creds.account_id:
            params["accountId"] = creds.account_id
        if filters:
            if filters.status is not None:
                native_status = to_native(filters.status)
                if native_status is None:
                    raise UnsupportedOperation(
                        detail=f"Tele2 Search Devices does not support status filter '{filters.status}'"
                    )
                params["status"] = native_status
            if filters.iccid:
                params["iccid"] = filters.iccid
            if filters.imsi:
                params["imsi"] = filters.imsi
            if filters.msisdn:
                params["msisdn"] = filters.msisdn
            for key, value in filters.custom.items():
                if not (
                    key.startswith("accountCustom")
                    or key.startswith("operatorCustom")
                    or key.startswith("customerCustom")
                ):
                    raise UnsupportedOperation(
                        detail=f"Tele2 Search Devices does not support custom filter '{key}'"
                    )
                params[key] = value

        data = await self._limited_get(credentials, creds, "/devices", params)
        devices = data.get("devices", [])
        if not isinstance(devices, list):
            devices = []

        enriched_devices: list[dict[str, Any]] = []
        for index, device in enumerate(devices):
            if not isinstance(device, dict):
                continue
            if index >= _DETAIL_ENRICHMENT_LIMIT:
                enriched_devices.append(cast(dict[str, Any], device))
                continue
            iccid = device.get("iccid")
            if not iccid:
                enriched_devices.append(cast(dict[str, Any], device))
                continue
            try:
                detail = await self._limited_get(
                    credentials,
                    creds,
                    f"/devices/{iccid}",
                )
            except Exception:
                fallback = dict(cast(dict[str, Any], device))
                fallback["detail_enriched"] = False
                enriched_devices.append(fallback)
                continue
            if isinstance(detail, dict):
                merged = dict(cast(dict[str, Any], device))
                merged.update(cast(dict[str, Any], detail))
                merged["detail_enriched"] = True
                enriched_devices.append(merged)
            else:
                enriched_devices.append(cast(dict[str, Any], device))
        subs = [_parse_subscription(d, company_id) for d in enriched_devices]
        if not data.get("lastPage", True):
            if till:
                next_cursor = f"page:{page + 1}|since:{since}|till:{till}"
            else:
                next_cursor = f"page:{page + 1}|since:{since}"
        else:
            next_cursor = None
        return subs, next_cursor
