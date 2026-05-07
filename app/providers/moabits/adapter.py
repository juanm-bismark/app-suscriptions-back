"""Moabits / Orion API 2.0 provider adapter — REST/JSON over HTTPS.

Credentials dict keys:
    base_url (str): v1 API base, e.g. "https://www.api.myorion.co".
    x_api_key (str): Orion application key. v1 paths exchange it for a Bearer
        token via GET /integrity/authorization-token. v2 paths
        (/api/v2/sim/{iccids}, /api/v2/sim/connectivity/{iccids}) accept the
        same key directly as the X-API-KEY header — no token exchange.
    company_codes (list[str]): Company codes owned by this tenant.
    company_id (str): Injected by the service layer — not a stored credential.

Provider-specific fields returned in Subscription.provider_fields (Moabits block):
    iccid, msisdn-side fields kept canonical at the top of Subscription.
    product_id, product_name, product_code, company_code, client_name,
    first_lu, first_cdr, last_lu, last_cdr, firstcdrmonth, last_network,
    imsi_number, imsi_raw, services_raw, services,
    number_of_renewals_plan, remaining_renewals_plan,
    plan_start_date, plan_expiration_date, autorenewal,
    data_limit_mb, sms_limit, sms_limit_mo, sms_limit_mt,
    data_service (Enabled|Disabled), sms_service (Enabled|Disabled).

Usage snapshot exposes Moabits-specific raw fields under provider_metrics:
    iccid, active_sim (bool|None), sms_mo (int|None), sms_mt (int|None),
    data_mb (int|None — Moabits returns data in MB).

Orion API 2.0.0 paths used here are documented in the public Swagger:
    GET /integrity/authorization-token
    GET /api/company/childs/{companyCode}
    GET /api/sim/details/{iccidList}
    GET /api/sim/serviceStatus/{iccidList}
    GET /api/usage/simUsage
    GET /api/company/simList/{companyCodes}
    GET /api/company/simListDetail/{companyCodes}
    GET /api/sim/connectivityStatus/{iccidList}
    GET /api/v2/sim/{iccidList}                 (v2, X-API-KEY directly)
    GET /api/v2/sim/connectivity/{iccidList}    (v2, X-API-KEY directly)
    PUT /api/sim/active/
    PUT /api/sim/suspend/
    PUT /api/sim/purge/
"""

import asyncio
import base64
import calendar
import dataclasses
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast

import httpx
import structlog

from app.config import get_settings
from app.providers.adapter_base import BaseAdapter
from app.providers.base import Provider
from app.shared.errors import (
    ProviderAuthFailed,
    ProviderForbidden,
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

from .status_map import map_status

_TOKEN_REFRESH_MARGIN_SECONDS = 300
_TOKEN_FALLBACK_TTL_SECONDS = 5 * 60 * 60 + 50 * 60
_TOKEN_STALE_GRACE_SECONDS = 30
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_LOCKS: dict[str, asyncio.Lock] = {}
MOABITS_DEFAULT_PARENT_COMPANY_CODE = "48123"

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _MoabitsCreds:
    base_url: str
    x_api_key: str
    company_codes: list[str]


def _creds(d: dict[str, Any]) -> _MoabitsCreds:
    codes = d.get("company_codes", [])
    if isinstance(codes, str):
        codes = [codes]
    x_api_key = d.get("x_api_key")
    if not x_api_key:
        raise ProviderAuthFailed(
            detail="Moabits credentials require x_api_key"
        )
    return _MoabitsCreds(
        base_url=d["base_url"].rstrip("/"),
        x_api_key=str(x_api_key),
        company_codes=codes,
    )


def _client(creds: _MoabitsCreds, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=creds.base_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=30.0,
    )


def _check(resp: httpx.Response, label: str = "Moabits") -> None:
    if resp.status_code == 401:
        if label == "Moabits authorization":
            raise ProviderAuthFailed(
                detail="Moabits x-api-key is absent, malformed, or not found"
            )
        raise ProviderAuthFailed(
            detail="Moabits authorization token is absent, expired, or invalid"
        )
    if resp.status_code == 403:
        if label == "Moabits authorization":
            raise ProviderAuthFailed(
                detail="Moabits x-api-key is cancelled, revoked, or expired"
            )
        raise ProviderForbidden(detail=f"{label} access denied")
    if resp.status_code == 429:
        raise ProviderRateLimited(detail=f"{label} rate limit exceeded")
    if resp.status_code >= 500:
        raise ProviderUnavailable(detail=f"{label} HTTP {resp.status_code}")
    if resp.status_code >= 400:
        raise ProviderProtocolError(
            detail=f"{label} HTTP {resp.status_code}: {resp.text[:200]}"
        )


def _parent_company_code(credentials: dict[str, Any], creds: _MoabitsCreds) -> str:
    raw = (
        credentials.get("parent_company_code")
        or credentials.get("root_company_code")
        or credentials.get("company_code")
    )
    if raw is None and creds.company_codes:
        raw = creds.company_codes[0]
    company_code = str(raw or MOABITS_DEFAULT_PARENT_COMPANY_CODE).strip()
    return company_code or MOABITS_DEFAULT_PARENT_COMPANY_CODE


async def fetch_child_companies(credentials: dict[str, Any]) -> list[dict[str, Any]]:
    """Return Moabits v1 child companies for the configured parent company."""
    creds = _creds(credentials)
    data = await _get(
        creds,
        f"/api/company/childs/{_parent_company_code(credentials, creds)}",
    )
    info = data.get("info") if isinstance(data, dict) else None
    rows = info.get("companyChilds") if isinstance(info, dict) else None
    if not isinstance(rows, list):
        raise ProviderProtocolError(
            detail="Moabits child companies response missing info.companyChilds"
        )
    return [row for row in rows if isinstance(row, dict)]


def _cache_key(creds: _MoabitsCreds) -> str:
    return f"{creds.base_url}|{creds.x_api_key}"


def _jwt_exp(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        exp = data.get("exp")
        return float(exp) if exp is not None else None
    except Exception:
        return None


def _token_expires_at(token: str) -> float:
    exp = _jwt_exp(token)
    if exp is not None:
        return exp
    return time.time() + _TOKEN_FALLBACK_TTL_SECONDS


async def _fetch_authorization_token(creds: _MoabitsCreds) -> str:
    async with httpx.AsyncClient(
        base_url=creds.base_url,
        headers={"X-API-KEY": creds.x_api_key, "Accept": "application/json"},
        timeout=30.0,
    ) as client:
        try:
            resp = await client.get("/integrity/authorization-token")
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(detail="Moabits authorization timeout") from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailable(
                detail=f"Moabits authorization network error: {exc}"
            ) from exc
    _check(resp, "Moabits authorization")
    data = resp.json()
    info = data.get("info") if isinstance(data, dict) else None
    token = info.get("authorizationToken") if isinstance(info, dict) else None
    if not token:
        raise ProviderProtocolError(
            detail="Moabits authorization response missing info.authorizationToken"
        )
    return str(token)


def _cached_token_is_usable(cached: tuple[str, float] | None) -> bool:
    if cached is None:
        return False
    return cached[1] + _TOKEN_STALE_GRACE_SECONDS > time.time()


async def _authorization_token(
    creds: _MoabitsCreds, *, force_refresh: bool = False
) -> str:
    key = _cache_key(creds)
    now = time.time()
    cached = _TOKEN_CACHE.get(key)
    if (
        not force_refresh
        and cached is not None
        and cached[1] - now > _TOKEN_REFRESH_MARGIN_SECONDS
    ):
        return cached[0]

    lock = _TOKEN_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        now = time.time()
        cached = _TOKEN_CACHE.get(key)
        if (
            not force_refresh
            and cached is not None
            and cached[1] - now > _TOKEN_REFRESH_MARGIN_SECONDS
        ):
            return cached[0]
        try:
            token = await _fetch_authorization_token(creds)
        except (ProviderAuthFailed, ProviderProtocolError, ProviderUnavailable):
            if cached is not None and _cached_token_is_usable(cached):
                return cached[0]
            raise
        _TOKEN_CACHE[key] = (token, _token_expires_at(token))
        return token


async def _get(creds: _MoabitsCreds, path: str, params: dict[str, Any] | None = None) -> Any:
    token = await _authorization_token(creds)
    async with _client(creds, token) as client:
        try:
            resp = await client.get(path, params=params or {})
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(detail="Moabits timeout") from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailable(detail=f"Moabits network error: {exc}") from exc
    if resp.status_code == 401:
        token = await _authorization_token(creds, force_refresh=True)
        async with _client(creds, token) as client:
            try:
                resp = await client.get(path, params=params or {})
            except httpx.TimeoutException as exc:
                raise ProviderUnavailable(detail="Moabits timeout") from exc
            except httpx.RequestError as exc:
                raise ProviderUnavailable(
                    detail=f"Moabits network error: {exc}"
                ) from exc
    _check(resp)
    return resp.json()


async def _put(creds: _MoabitsCreds, path: str, body: dict[str, Any], idempotency_key: str | None = None) -> Any:
    token = await _authorization_token(creds)
    async with _client(creds, token) as client:
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            resp = await client.put(path, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(detail="Moabits timeout") from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailable(detail=f"Moabits network error: {exc}") from exc
    if resp.status_code == 401:
        token = await _authorization_token(creds, force_refresh=True)
        async with _client(creds, token) as client:
            headers = {}
            if idempotency_key:
                headers["Idempotency-Key"] = idempotency_key
            try:
                resp = await client.put(path, json=body, headers=headers)
            except httpx.TimeoutException as exc:
                raise ProviderUnavailable(detail="Moabits timeout") from exc
            except httpx.RequestError as exc:
                raise ProviderUnavailable(
                    detail=f"Moabits network error: {exc}"
                ) from exc
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


def _list_from(data: Any, *keys: str) -> list[Any]:
    """Navigate nested dict keys and return the final list, or an empty list."""
    node: Any = data
    for key in keys:
        if not isinstance(node, dict):
            return []
        node = cast(dict[str, Any], node).get(key)
    return node if isinstance(node, list) else []


def _status_row_from(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        return {"iccid": value.strip()}
    return None


def _service_fields_from_status_row(row: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {"detail_enriched": False}
    enabled_services: list[str] = []
    if (v := row.get("dataService")) is not None:
        fields["data_service"] = v
        if str(v).strip().lower() == "enabled":
            enabled_services.append("data")
    if (v := row.get("smsService")) is not None:
        fields["sms_service"] = v
        if str(v).strip().lower() == "enabled":
            enabled_services.append("sms")
    if "data_service" in fields or "sms_service" in fields:
        fields["services"] = enabled_services
    return fields


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


@dataclass(frozen=True)
class _V2Settings:
    enabled: bool
    base_url: str
    max_batch: int
    max_concurrent_chunks: int
    detail_timeout_seconds: float
    connectivity_timeout_seconds: float


def _v2_settings_from_app_settings() -> _V2Settings:
    s = get_settings()
    return _V2Settings(
        enabled=s.moabits_v2_enrichment_enabled,
        base_url=s.moabits_v2_base_url.rstrip("/"),
        max_batch=max(int(s.moabits_v2_max_batch), 1),
        max_concurrent_chunks=max(int(s.moabits_v2_max_concurrent_chunks), 1),
        detail_timeout_seconds=float(s.moabits_v2_detail_timeout_seconds),
        connectivity_timeout_seconds=float(s.moabits_v2_connectivity_timeout_seconds),
    )


def _v2_check(resp: httpx.Response, label: str) -> bool:
    """Return True for 200, False for 404 (treated as 'not found, empty result').

    Other statuses raise canonical provider errors. 401 is fatal: a misconfigured
    X-API-KEY would degrade every call silently otherwise.
    """
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    if resp.status_code == 401:
        raise ProviderAuthFailed(
            detail=f"Moabits v2 X-API-KEY is invalid or missing ({label})"
        )
    if resp.status_code == 403:
        raise ProviderForbidden(
            detail=f"Moabits v2 {label} access denied"
        )
    if resp.status_code == 429:
        raise ProviderRateLimited(
            detail=f"Moabits v2 {label} rate limit exceeded"
        )
    if resp.status_code >= 500:
        raise ProviderUnavailable(
            detail=f"Moabits v2 {label} HTTP {resp.status_code}"
        )
    if resp.status_code >= 400:
        raise ProviderProtocolError(
            detail=f"Moabits v2 {label} HTTP {resp.status_code}: {resp.text[:200]}"
        )
    return True


async def _v2_get(
    base_url: str, x_api_key: str, path: str, *, timeout: float, label: str
) -> Any | None:
    """v2 GET using X-API-KEY directly. Returns parsed JSON, or None on 404.

    No Bearer token exchange — v2 endpoints accept the application key as-is.
    Raises ProviderUnavailable on network/timeouts.
    """
    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"X-API-KEY": x_api_key, "Accept": "application/json"},
        timeout=timeout,
    ) as client:
        try:
            resp = await client.get(path)
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(
                detail=f"Moabits v2 {label} timeout"
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailable(
                detail=f"Moabits v2 {label} network error: {exc}"
            ) from exc
    if not _v2_check(resp, label):
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderProtocolError(
            detail=f"Moabits v2 {label} non-JSON response"
        ) from exc


async def _v2_fetch_details_chunk(
    base_url: str, x_api_key: str, iccids: list[str], timeout: float
) -> dict[str, dict[str, Any]]:
    """Fetch a single batch of v2 sim details. Returns {iccid: simInfo row}.

    404 from v2 means none of the iccids in the batch exist there → {}.
    """
    if not iccids:
        return {}
    path = f"/api/v2/sim/{','.join(iccids)}"
    data = await _v2_get(
        base_url, x_api_key, path, timeout=timeout, label="sim detail"
    )
    if data is None:
        return {}
    rows = _list_from(data, "info", "simInfo")
    return {
        str(row["iccid"]): row
        for row in rows
        if isinstance(row, dict) and row.get("iccid")
    }


async def _v2_fetch_connectivity_chunk(
    base_url: str, x_api_key: str, iccids: list[str], timeout: float
) -> dict[str, dict[str, Any]]:
    """Fetch a single batch of v2 connectivity. Returns {iccid: row}.

    The published v2 example returns a top-level array; tolerate also the
    legacy v1-style {info.connectivityStatus[]} envelope and a single-object
    response (in case the documented schema is delivered for a 1-iccid query).
    """
    if not iccids:
        return {}
    path = f"/api/v2/sim/connectivity/{','.join(iccids)}"
    data = await _v2_get(
        base_url, x_api_key, path, timeout=timeout, label="sim connectivity"
    )
    if data is None:
        return {}
    rows: list[Any]
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        info_rows = _list_from(data, "info", "connectivityStatus")
        if info_rows:
            rows = info_rows
        elif data.get("iccid"):
            rows = [data]
        else:
            rows = []
    else:
        rows = []
    return {
        str(row["iccid"]): row
        for row in rows
        if isinstance(row, dict) and row.get("iccid")
    }


async def _fetch_v2_enrichment(
    *,
    v2_base_url: str,
    x_api_key: str,
    iccids: list[str],
    settings: _V2Settings,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Fetch v2 details + connectivity in parallel, chunked, with graceful degradation.

    Failures (timeouts, 5xx, 401, 403, rate limits) are logged and produce an
    empty map for the affected chunk. A failure on one chunk does NOT abort
    sibling chunks — callers see the union of what succeeded and decide
    per-iccid whether enrichment is full, partial, or missing.
    """
    if not iccids:
        return {}, {}
    chunks = [
        iccids[i : i + settings.max_batch]
        for i in range(0, len(iccids), settings.max_batch)
    ]
    sem = asyncio.Semaphore(settings.max_concurrent_chunks)

    swallow = (
        ProviderUnavailable,
        ProviderProtocolError,
        ProviderRateLimited,
        ProviderForbidden,
        ProviderAuthFailed,
    )

    async def _detail(chunk: list[str]) -> dict[str, dict[str, Any]]:
        async with sem:
            try:
                return await _v2_fetch_details_chunk(
                    v2_base_url,
                    x_api_key,
                    chunk,
                    settings.detail_timeout_seconds,
                )
            except swallow as exc:
                logger.warning(
                    "moabits_v2_enrichment_chunk_failed",
                    label="sim_detail",
                    chunk_size=len(chunk),
                    error=str(exc),
                )
                return {}

    async def _conn(chunk: list[str]) -> dict[str, dict[str, Any]]:
        async with sem:
            try:
                return await _v2_fetch_connectivity_chunk(
                    v2_base_url,
                    x_api_key,
                    chunk,
                    settings.connectivity_timeout_seconds,
                )
            except swallow as exc:
                logger.warning(
                    "moabits_v2_enrichment_chunk_failed",
                    label="sim_connectivity",
                    chunk_size=len(chunk),
                    error=str(exc),
                )
                return {}

    detail_lists, conn_lists = await asyncio.gather(
        asyncio.gather(*(_detail(c) for c in chunks)),
        asyncio.gather(*(_conn(c) for c in chunks)),
    )

    detail_map: dict[str, dict[str, Any]] = {}
    for d in detail_lists:
        detail_map.update(d)
    conn_map: dict[str, dict[str, Any]] = {}
    for c in conn_lists:
        conn_map.update(c)
    return detail_map, conn_map


_V2_CONNECTIVITY_FIELD_MAP: dict[str, str] = {
    "network": "operator",
    "country": "country",
    "rat": "rat_type",
    "privateIp": "ip_address",
    "mcc": "mcc",
    "mnc": "mnc",
    "chargeTowards": "charge_towards",
    "dataSessionId": "data_session_id",
    "dateOpened": "session_started_at",
    "usageKB": "usage_kb",
    "imsi": "connectivity_imsi_raw",
}


def _apply_v2_connectivity(pf: dict[str, Any], conn: dict[str, Any]) -> None:
    """Map v2 connectivity payload onto provider_fields with canonical keys.

    Keys here align with the `network.*` block built in
    `app/subscriptions/routers/sims.py::_normalized_subscription`. Empty
    strings and None are dropped so a partial payload does not overwrite
    information already supplied by v1 or v2 detail.
    """
    for src, dst in _V2_CONNECTIVITY_FIELD_MAP.items():
        v = conn.get(src)
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if src == "usageKB":
            v = _coerce_int(v)
            if v is None:
                continue
        pf[dst] = v


def _build_listing_subscription(
    *,
    v1_row: dict[str, Any],
    v2_detail: dict[str, Any] | None,
    v2_connectivity: dict[str, Any] | None,
    v2_attempted: bool,
    company_id: str,
) -> Subscription:
    """Build a Subscription from v1 simList row plus optional v2 enrichment.

    Layering rules:
    - v1 is the authoritative source of `simStatus` and current service flags
      (dataService/smsService) — the active-state view.
    - v2 detail provides identity (msisdn, imsi, imei), plan, customer, dates.
      If absent, the Subscription stays at `detail_level=summary`.
    - v2 connectivity provides real-time network info. If absent, network.*
      stays empty; callers must not assume it.
    - `services` (active list) is derived from v1's enabled/disabled flags, not
      from v2 detail's administrative `services` string. v2's `services_raw` is
      kept under provider_fields for inspection.

    `v2_attempted=False` means the v2 enrichment flag is off; in that case the
    output is identical to the v1-only legacy listing (no `enrichment_status`
    key, no v2-derived fields). This preserves the pre-flag contract exactly.
    """
    iccid = str(v1_row.get("iccid") or "")

    if not v2_attempted:
        native_status = v1_row.get("simStatus", "Unknown")
        return Subscription(
            iccid=iccid,
            msisdn=None,
            imsi=None,
            status=map_status(native_status),
            native_status=native_status,
            provider=Provider.MOABITS.value,
            company_id=company_id,
            activated_at=None,
            updated_at=None,
            provider_fields=_service_fields_from_status_row(v1_row),
        )

    sub = _build_subscription(v2_detail or {}, v1_row, iccid, company_id)

    pf: dict[str, Any] = dict(sub.provider_fields)
    v1_service_fields = _service_fields_from_status_row(v1_row)
    if "services" in v1_service_fields:
        pf["services"] = v1_service_fields["services"]
    if "data_service" in v1_service_fields:
        pf["data_service"] = v1_service_fields["data_service"]
    if "sms_service" in v1_service_fields:
        pf["sms_service"] = v1_service_fields["sms_service"]

    if v2_connectivity:
        _apply_v2_connectivity(pf, v2_connectivity)

    pf["detail_enriched"] = v2_detail is not None
    if v2_detail and v2_connectivity:
        pf["enrichment_status"] = "full"
    elif v2_detail:
        pf["enrichment_status"] = "detail_only"
    elif v2_connectivity:
        pf["enrichment_status"] = "connectivity_only"
    else:
        pf["enrichment_status"] = "v1_only"

    return dataclasses.replace(sub, provider_fields=pf)


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
        ("imsi", "imsi_raw"),
        ("imsiNumber", "imsi_number"),
        ("first_lu", "first_lu"),
        ("first_cdr", "first_cdr"),
        ("last_lu", "last_lu"),
        ("last_cdr", "last_cdr"),
        ("firstcdrmonth", "firstcdrmonth"),
        ("imei", "imei"),
        ("autorenewal", "autorenewal"),
        ("numberOfRenewalsPlan", "number_of_renewals_plan"),
        ("remainingRenewalsPlan", "remaining_renewals_plan"),
        ("planStartDate", "plan_start_date"),
        ("planExpirationDate", "plan_expiration_date"),
    ]:
        if (v := sim_info.get(src_key)) is not None:
            provider_fields[dst_key] = v

    for src_key, dst_key in [
        ("dataLimit", "data_limit_mb"),
        ("smsLimit", "sms_limit"),
        ("smsLimitMo", "sms_limit_mo"),
        ("smsLimitMt", "sms_limit_mt"),
    ]:
        if src_key in sim_info and (v := _coerce_int(sim_info.get(src_key))) is not None:
            provider_fields[dst_key] = v

    sms_limit_parts = [
        provider_fields.get("sms_limit_mo"),
        provider_fields.get("sms_limit_mt"),
    ]
    if "sms_limit" not in provider_fields and any(v is not None for v in sms_limit_parts):
        provider_fields["sms_limit"] = sum(v or 0 for v in sms_limit_parts)

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
        imsi=sim_info.get("imsiNumber") or sim_info.get("imsi"),
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


def _add_months(value: datetime, months: int) -> datetime:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


class MoabitsAdapter(BaseAdapter):
    """Moabits / Orion API adapter with circuit breaker (ADR-005).

    Stateless — one singleton in the registry.
    Circuit breaker: opens after 5 failures in 30s window, stays open for 30s.
    """

    def __init__(self) -> None:
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
        now = datetime.now(tz=UTC)
        start = start_date or now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = end_date or now
        if end < start:
            raise ProviderValidationError(detail="end_date must be after start_date")
        if end > _add_months(start, 6):
            raise ProviderValidationError(
                detail="Moabits usage date range cannot exceed 6 months"
            )
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
        if not get_settings().lifecycle_writes_enabled:
            raise UnsupportedOperation(
                detail="Lifecycle write operations are disabled by feature flag"
            )
        if target not in {AdministrativeStatus.ACTIVE, AdministrativeStatus.SUSPENDED}:
            raise UnsupportedOperation(
                detail=f"Moabits only supports active/suspended service writes, got '{target}'"
            )

        data_enabled = bool(data_service)
        sms_enabled = bool(sms_service)
        if not data_enabled and not sms_enabled:
            action = "active" if target == AdministrativeStatus.ACTIVE else "suspend"
            raise ProviderValidationError(detail=f"No service to {action}")

        path = (
            "/api/sim/active/"
            if target == AdministrativeStatus.ACTIVE
            else "/api/sim/suspend/"
        )
        creds = _creds(credentials)
        await _put(
            creds,
            path,
            {
                "iccidList": [iccid],
                "dataService": data_enabled,
                "smsService": sms_enabled,
            },
            idempotency_key=idempotency_key,
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
        if filters and (
            filters.status is not None
            or bool(filters.iccid)
            or bool(filters.imsi)
            or bool(filters.msisdn)
            or bool(filters.custom)
        ):
            raise UnsupportedOperation(
                detail="Moabits Search Devices filters are not documented"
            )
        creds = _creds(credentials)
        company_id = credentials.get("company_id", "")
        if not creds.company_codes:
            return [], None

        try:
            offset = max(int(cursor or "0"), 0)
        except ValueError:
            offset = 0
        page_size = min(max(limit, 1), 500)
        page: list[dict[str, Any]] = []
        rows_seen = 0
        has_more = False

        for company_index, company_code in enumerate(creds.company_codes):
            status_data = await _get(creds, f"/api/company/simList/{company_code}")
            rows = [
                row
                for row in (
                    _status_row_from(value)
                    for value in _list_from(status_data, "info", "iccidList")
                )
                if row is not None and row.get("iccid")
            ]
            if rows_seen + len(rows) <= offset:
                rows_seen += len(rows)
                continue

            start = max(offset - rows_seen, 0)
            remaining = page_size - len(page)
            page.extend(rows[start : start + remaining])
            rows_seen += len(rows)

            if len(page) >= page_size:
                has_more_in_company = start + remaining < len(rows)
                has_more_companies = company_index < len(creds.company_codes) - 1
                has_more = has_more_in_company or has_more_companies
                break

        v2_settings = _v2_settings_from_app_settings()
        detail_map: dict[str, dict[str, Any]] = {}
        conn_map: dict[str, dict[str, Any]] = {}
        if v2_settings.enabled and page:
            page_iccids = [
                str(row["iccid"]) for row in page if row.get("iccid")
            ]
            detail_map, conn_map = await _fetch_v2_enrichment(
                v2_base_url=v2_settings.base_url,
                x_api_key=creds.x_api_key,
                iccids=page_iccids,
                settings=v2_settings,
            )

        subs: list[Subscription] = []
        for row in page:
            iccid = str(row.get("iccid") or "")
            subs.append(
                _build_listing_subscription(
                    v1_row=row,
                    v2_detail=detail_map.get(iccid),
                    v2_connectivity=conn_map.get(iccid),
                    v2_attempted=v2_settings.enabled,
                    company_id=company_id,
                )
            )

        next_cursor = str(offset + len(page)) if has_more else None
        return subs, next_cursor
