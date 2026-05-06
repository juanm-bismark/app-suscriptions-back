"""Unit tests for the Moabits provider adapter.

These tests use realistic Moabits/Orion API payloads to validate that:
- get_subscription extracts the full provider_fields contract
  (iccid, lastNetwork, clientName, imsiNumber, firstcdrmonth, services).
- services="data/sms" is normalized to a list while services_raw is preserved.
- get_usage exposes activeSim, smsMO, smsMT, data and iccid via provider_metrics.
- get_presence reads from getConnectivityStatus and exposes rat (rat_type).
- Optional fields (rat/network/country/lastNetwork/clientName/etc.) are tolerated
  when missing or null.
- The unified status / native_status / provider_fields contract is preserved.
"""

import re
import base64
import json
import time
from decimal import Decimal
from datetime import UTC, datetime

import pytest

from app.providers.moabits import adapter as moabits_adapter_mod
from app.providers.moabits.adapter import (
    MoabitsAdapter,
    _coerce_bool,
    _coerce_int,
    _normalize_services,
)
from app.subscriptions.domain import (
    AdministrativeStatus,
    ConnectivityState,
    SubscriptionSearchFilters,
)
from app.shared.errors import (
    ProviderAuthFailed,
    ProviderValidationError,
    UnsupportedOperation,
)
from app.providers.moabits.status_map import map_status

# Matches /api/usage/simUsage with any querystring (Moabits adds date params).
_USAGE_URL_RE = re.compile(r"^https://api\.moabits\.test/api/usage/simUsage(\?.*)?$")


@pytest.fixture(autouse=True)
def _clear_moabits_token_cache() -> None:
    moabits_adapter_mod._TOKEN_CACHE.clear()
    moabits_adapter_mod._TOKEN_LOCKS.clear()


def _jwt_with_exp(exp: int) -> str:
    def _b64(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{_b64({'alg': 'none'})}.{_b64({'exp': exp})}.sig"


# ── helpers ─────────────────────────────────────────────────────────────────────

def _details_payload(extra: dict | None = None) -> dict:
    """Realistic getSimDetails payload (info.simInfo[])."""
    sim_info = {
        "iccid": "8934070100000000001",
        "msisdn": "346000000001",
        "imsi": "214070000000001",
        "imsiNumber": "214070000000001",
        "lastNetwork": "Movistar ES",
        "clientName": "ACME Logistics",
        "services": "data/sms",
        "firstcdrmonth": "2024-08",
        "first_lu": "2024-08-01 10:00:00",
        "first_cdr": "2024-08-01 12:30:00",
        "last_lu": "2026-04-28 09:00:00",
        "last_cdr": "2026-04-29T08:15:00Z",
        "imei": "359000000000001",
        "autorenewal": True,
        "product_name": "IoT Plan 100MB",
        "product_code": "IOT-100",
        "product_id": "p-100",
        "companyCode": "ACME",
        "dataLimit": 100,
        "smsLimit": 50,
        "numberOfRenewalsPlan": 12,
        "remainingRenewalsPlan": 8,
        "planStartDate": "2024-08-01T00:00:00Z",
        "planExpirationDate": "2026-08-01T00:00:00Z",
    }
    if extra:
        sim_info.update(extra)
    return {"info": {"simInfo": [sim_info]}}


def _service_status_payload() -> dict:
    return {
        "info": {
            "iccidList": [
                {
                    "iccid": "8934070100000000001",
                    "simStatus": "Active",
                    "dataService": "Enabled",
                    "smsService": "Enabled",
                }
            ]
        }
    }


def _usage_payload(extra: dict | None = None) -> dict:
    row = {
        "iccid": "8934070100000000001",
        "activeSim": True,
        "smsMO": 4,
        "smsMT": 2,
        "data": 137,  # MB
    }
    if extra:
        row.update(extra)
    return {"info": {"simsUsage": [row]}}


def _connectivity_payload(extra: dict | None = None) -> dict:
    row = {
        "iccid": "8934070100000000001",
        "status": "Online",
        "country": "ES",
        "rat": "LTE",
        "network": "Movistar",
    }
    if extra:
        row.update(extra)
    return {"info": {"connectivityStatus": [row]}}


# ── pure helpers ────────────────────────────────────────────────────────────────

class TestNormalizeServices:
    def test_slash_separated_string(self) -> None:
        assert _normalize_services("data/sms") == ["data", "sms"]

    def test_with_whitespace(self) -> None:
        assert _normalize_services("  data / sms  ") == ["data", "sms"]

    def test_single_value(self) -> None:
        assert _normalize_services("data") == ["data"]

    def test_none(self) -> None:
        assert _normalize_services(None) is None

    def test_empty_string(self) -> None:
        assert _normalize_services("") is None
        assert _normalize_services("   ") is None

    def test_list_fallback(self) -> None:
        assert _normalize_services(["DATA", "sms"]) == ["data", "sms"]

    def test_list_with_empty_entries(self) -> None:
        assert _normalize_services(["data", "", None]) == ["data"]


class TestCoercers:
    def test_int_string(self) -> None:
        assert _coerce_int("42") == 42

    def test_int_float_string(self) -> None:
        assert _coerce_int("4.0") == 4

    def test_int_none(self) -> None:
        assert _coerce_int(None) is None
        assert _coerce_int("") is None
        assert _coerce_int("abc") is None

    def test_bool_true(self) -> None:
        assert _coerce_bool(True) is True
        assert _coerce_bool("true") is True
        assert _coerce_bool("Active") is True
        assert _coerce_bool(1) is True

    def test_bool_false(self) -> None:
        assert _coerce_bool(False) is False
        assert _coerce_bool("false") is False
        assert _coerce_bool(0) is False

    def test_bool_none(self) -> None:
        assert _coerce_bool(None) is None
        assert _coerce_bool("garbage") is None


# ── get_subscription ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_subscription_extracts_full_provider_fields(
    httpx_mock, moabits_creds: dict
) -> None:
    iccid = "8934070100000000001"
    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/details/{iccid}",
        json=_details_payload(),
    )
    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/serviceStatus/{iccid}",
        json=_service_status_payload(),
    )

    sub = await MoabitsAdapter().get_subscription(iccid, moabits_creds)

    pf = sub.provider_fields
    # Newly required fields
    assert pf["iccid"] == iccid
    assert pf["last_network"] == "Movistar ES"
    assert pf["client_name"] == "ACME Logistics"
    assert pf["imsi_number"] == "214070000000001"
    assert pf["firstcdrmonth"] == "2024-08"
    # services normalization
    assert pf["services_raw"] == "data/sms"
    assert pf["services"] == ["data", "sms"]
    # Pre-existing fields preserved
    assert pf["product_name"] == "IoT Plan 100MB"
    assert pf["company_code"] == "ACME"
    assert pf["data_limit_mb"] == 100
    assert pf["data_service"] == "Enabled"
    assert pf["sms_service"] == "Enabled"
    # Common contract preserved
    assert sub.iccid == iccid
    assert sub.msisdn == "346000000001"
    assert sub.imsi == "214070000000001"
    assert sub.status == AdministrativeStatus.ACTIVE
    assert sub.native_status == "Active"
    assert sub.provider == "moabits"


@pytest.mark.asyncio
async def test_get_subscription_services_null_does_not_break(
    httpx_mock, moabits_creds: dict
) -> None:
    iccid = "8934070100000000001"
    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/details/{iccid}",
        json=_details_payload(extra={"services": None}),
    )
    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/serviceStatus/{iccid}",
        json=_service_status_payload(),
    )

    sub = await MoabitsAdapter().get_subscription(iccid, moabits_creds)
    pf = sub.provider_fields
    assert pf.get("services_raw") is None
    assert "services" not in pf  # no normalized list when raw is null


@pytest.mark.asyncio
async def test_get_subscription_optional_fields_missing_ok(
    httpx_mock, moabits_creds: dict
) -> None:
    """Missing optional fields must not break extraction."""
    iccid = "8934070100000000001"
    minimal_info = {
        "iccid": iccid,
        # explicitly omit lastNetwork, clientName, imsiNumber, firstcdrmonth, services
    }
    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/details/{iccid}",
        json={"info": {"simInfo": [minimal_info]}},
    )
    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/serviceStatus/{iccid}",
        json=_service_status_payload(),
    )

    sub = await MoabitsAdapter().get_subscription(iccid, moabits_creds)
    pf = sub.provider_fields
    assert pf["iccid"] == iccid
    for absent in (
        "last_network",
        "client_name",
        "imsi_number",
        "firstcdrmonth",
        "services_raw",
        "services",
    ):
        assert absent not in pf
    # Common contract still works
    assert sub.status == AdministrativeStatus.ACTIVE
    assert sub.native_status == "Active"


# ── get_usage ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_usage_exposes_active_sim_and_iccid(
    httpx_mock, moabits_creds: dict
) -> None:
    iccid = "8934070100000000001"
    httpx_mock.add_response(
        url=_USAGE_URL_RE,
        json=_usage_payload(),
    )

    snap = await MoabitsAdapter().get_usage(iccid, moabits_creds)

    assert snap.iccid == iccid
    assert snap.sms_count == 6
    assert snap.voice_seconds == 0
    assert snap.data_used_bytes == Decimal(137 * 1024 * 1024)
    pm = snap.provider_metrics
    assert pm["iccid"] == iccid
    assert pm["active_sim"] is True
    assert pm["sms_mo"] == 4
    assert pm["sms_mt"] == 2
    assert pm["data_mb"] == 137


@pytest.mark.asyncio
async def test_get_usage_can_exchange_application_key_for_jwt(httpx_mock) -> None:
    iccid = "8934070100000000001"
    httpx_mock.add_response(
        url="https://api.moabits.test/integrity/authorization-token",
        json={
            "status": "Ok",
            "info": {"authorizationToken": _jwt_with_exp(int(time.time()) + 3600)},
        },
    )
    httpx_mock.add_response(
        url=_USAGE_URL_RE,
        json=_usage_payload(),
    )

    await MoabitsAdapter().get_usage(
        iccid,
        {
            "base_url": "https://api.moabits.test",
            "application_key": "app-key",
            "company_codes": ["ACME"],
        },
    )

    auth_request, usage_request = httpx_mock.get_requests()
    assert auth_request.headers["x-api-key"] == "app-key"
    assert usage_request.headers["authorization"].startswith("Bearer ")


@pytest.mark.asyncio
async def test_get_usage_reuses_cached_jwt_until_refresh_window(httpx_mock) -> None:
    iccid = "8934070100000000001"
    httpx_mock.add_response(
        url="https://api.moabits.test/integrity/authorization-token",
        json={
            "status": "Ok",
            "info": {"authorizationToken": _jwt_with_exp(int(time.time()) + 3600)},
        },
    )
    httpx_mock.add_response(url=_USAGE_URL_RE, json=_usage_payload())
    httpx_mock.add_response(url=_USAGE_URL_RE, json=_usage_payload())
    creds = {
        "base_url": "https://api.moabits.test",
        "application_key": "app-key",
        "company_codes": ["ACME"],
    }

    await MoabitsAdapter().get_usage(iccid, creds)
    await MoabitsAdapter().get_usage(iccid, creds)

    requests = httpx_mock.get_requests()
    assert [request.url.path for request in requests].count(
        "/integrity/authorization-token"
    ) == 1
    assert [request.url.path for request in requests].count("/api/usage/simUsage") == 2


@pytest.mark.asyncio
async def test_get_usage_refreshes_jwt_when_exp_is_near(httpx_mock) -> None:
    iccid = "8934070100000000001"
    httpx_mock.add_response(
        url="https://api.moabits.test/integrity/authorization-token",
        json={
            "status": "Ok",
            "info": {"authorizationToken": _jwt_with_exp(int(time.time()) + 60)},
        },
    )
    httpx_mock.add_response(url=_USAGE_URL_RE, json=_usage_payload())
    httpx_mock.add_response(
        url="https://api.moabits.test/integrity/authorization-token",
        json={
            "status": "Ok",
            "info": {"authorizationToken": _jwt_with_exp(int(time.time()) + 3600)},
        },
    )
    httpx_mock.add_response(url=_USAGE_URL_RE, json=_usage_payload())
    creds = {
        "base_url": "https://api.moabits.test",
        "application_key": "app-key",
        "company_codes": ["ACME"],
    }

    await MoabitsAdapter().get_usage(iccid, creds)
    await MoabitsAdapter().get_usage(iccid, creds)

    assert [request.url.path for request in httpx_mock.get_requests()].count(
        "/integrity/authorization-token"
    ) == 2


@pytest.mark.asyncio
async def test_get_usage_refreshes_and_retries_once_on_business_401(httpx_mock) -> None:
    iccid = "8934070100000000001"
    first_token = _jwt_with_exp(int(time.time()) + 3600)
    second_token = _jwt_with_exp(int(time.time()) + 7200)
    httpx_mock.add_response(
        url="https://api.moabits.test/integrity/authorization-token",
        json={"status": "Ok", "info": {"authorizationToken": first_token}},
    )
    httpx_mock.add_response(url=_USAGE_URL_RE, status_code=401, text="Absent authorization")
    httpx_mock.add_response(
        url="https://api.moabits.test/integrity/authorization-token",
        json={"status": "Ok", "info": {"authorizationToken": second_token}},
    )
    httpx_mock.add_response(url=_USAGE_URL_RE, json=_usage_payload())

    await MoabitsAdapter().get_usage(
        iccid,
        {
            "base_url": "https://api.moabits.test",
            "application_key": "app-key",
            "company_codes": ["ACME"],
        },
    )

    usage_requests = [
        request
        for request in httpx_mock.get_requests()
        if request.url.path == "/api/usage/simUsage"
    ]
    assert len(usage_requests) == 2
    assert usage_requests[0].headers["authorization"] == f"Bearer {first_token}"
    assert usage_requests[1].headers["authorization"] == f"Bearer {second_token}"


@pytest.mark.asyncio
async def test_get_usage_token_endpoint_403_is_credential_error(httpx_mock) -> None:
    httpx_mock.add_response(
        url="https://api.moabits.test/integrity/authorization-token",
        status_code=403,
        text="The api key is Cancelled / Revoked / Expired",
    )

    with pytest.raises(ProviderAuthFailed) as excinfo:
        await MoabitsAdapter().get_usage(
            "8934070100000000001",
            {
                "base_url": "https://api.moabits.test",
                "application_key": "app-key",
                "company_codes": ["ACME"],
            },
        )

    assert "x-api-key is cancelled" in excinfo.value.detail


@pytest.mark.asyncio
async def test_get_usage_handles_missing_optional_fields(
    httpx_mock, moabits_creds: dict
) -> None:
    iccid = "8934070100000000001"
    httpx_mock.add_response(
        url=_USAGE_URL_RE,
        json={
            "info": {
                "simsUsage": [
                    {
                        "iccid": iccid,
                        # activeSim, smsMT, data are absent
                        "smsMO": 0,
                    }
                ]
            }
        },
    )

    snap = await MoabitsAdapter().get_usage(iccid, moabits_creds)
    pm = snap.provider_metrics
    assert pm["active_sim"] is None
    assert pm["sms_mt"] is None
    assert pm["data_mb"] is None
    assert pm["sms_mo"] == 0


@pytest.mark.asyncio
async def test_get_usage_rejects_ranges_longer_than_six_months(
    moabits_creds: dict,
) -> None:
    with pytest.raises(ProviderValidationError) as excinfo:
        await MoabitsAdapter().get_usage(
            "8934070100000000001",
            moabits_creds,
            start_date=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 7, 2, 0, 0, 0, tzinfo=UTC),
        )

    assert "cannot exceed 6 months" in excinfo.value.detail


# ── get_presence ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_presence_uses_connectivity_status_and_exposes_rat(
    httpx_mock, moabits_creds: dict
) -> None:
    iccid = "8934070100000000001"
    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/connectivityStatus/{iccid}",
        json=_connectivity_payload(),
    )

    presence = await MoabitsAdapter().get_presence(iccid, moabits_creds)
    assert presence.state == ConnectivityState.ONLINE
    assert presence.country_code == "ES"
    assert presence.network_name == "Movistar"
    assert presence.rat_type == "LTE"
    assert presence.iccid == iccid


@pytest.mark.asyncio
async def test_get_presence_offline_state(
    httpx_mock, moabits_creds: dict
) -> None:
    iccid = "8934070100000000001"
    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/connectivityStatus/{iccid}",
        json=_connectivity_payload(extra={"status": "offline"}),
    )

    presence = await MoabitsAdapter().get_presence(iccid, moabits_creds)
    assert presence.state == ConnectivityState.OFFLINE


@pytest.mark.asyncio
async def test_get_presence_tolerates_missing_optional_fields(
    httpx_mock, moabits_creds: dict
) -> None:
    iccid = "8934070100000000001"
    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/connectivityStatus/{iccid}",
        json={
            "info": {
                "connectivityStatus": [
                    {
                        "iccid": iccid,
                        "status": "Online",
                        # country, rat, network are absent
                    }
                ]
            }
        },
    )

    presence = await MoabitsAdapter().get_presence(iccid, moabits_creds)
    assert presence.state == ConnectivityState.ONLINE
    assert presence.country_code is None
    assert presence.rat_type is None
    assert presence.network_name is None


@pytest.mark.asyncio
async def test_get_presence_treats_blank_optionals_as_none(
    httpx_mock, moabits_creds: dict
) -> None:
    iccid = "8934070100000000001"
    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/connectivityStatus/{iccid}",
        json=_connectivity_payload(extra={"rat": "", "network": None, "country": "  "}),
    )

    presence = await MoabitsAdapter().get_presence(iccid, moabits_creds)
    assert presence.rat_type is None
    assert presence.network_name is None
    assert presence.country_code is None


@pytest.mark.asyncio
async def test_get_presence_unknown_status(
    httpx_mock, moabits_creds: dict
) -> None:
    iccid = "8934070100000000001"
    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/connectivityStatus/{iccid}",
        json={
            "info": {
                "connectivityStatus": [
                    {"iccid": iccid, "status": None}
                ]
            }
        },
    )

    presence = await MoabitsAdapter().get_presence(iccid, moabits_creds)
    assert presence.state == ConnectivityState.UNKNOWN


# ── status / native_status contract ─────────────────────────────────────────────


def test_status_map_accepts_documented_and_observed_values() -> None:
    assert map_status("ACTIVATED") == AdministrativeStatus.ACTIVE
    assert map_status("Active") == AdministrativeStatus.ACTIVE
    assert map_status("TEST_READY") == AdministrativeStatus.IN_TEST
    assert map_status("Ready") == AdministrativeStatus.IN_TEST
    assert map_status("SUSPENDED") == AdministrativeStatus.SUSPENDED
    assert map_status("Suspended") == AdministrativeStatus.SUSPENDED
    assert map_status("PURGED") == AdministrativeStatus.PURGED
    assert map_status("INVENTORY") == AdministrativeStatus.INVENTORY
    assert map_status("DEACTIVATED") == AdministrativeStatus.TERMINATED

@pytest.mark.asyncio
async def test_native_status_preserved_alongside_unified_status(
    httpx_mock, moabits_creds: dict
) -> None:
    iccid = "8934070100000000001"
    payload = _service_status_payload()
    payload["info"]["iccidList"][0]["simStatus"] = "Suspended"

    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/details/{iccid}",
        json=_details_payload(),
    )
    httpx_mock.add_response(
        url=f"https://api.moabits.test/api/sim/serviceStatus/{iccid}",
        json=payload,
    )

    sub = await MoabitsAdapter().get_subscription(iccid, moabits_creds)
    assert sub.native_status == "Suspended"
    assert sub.status == AdministrativeStatus.SUSPENDED


@pytest.mark.asyncio
async def test_list_subscriptions_ignores_modified_date_filters(
    httpx_mock, moabits_creds: dict
) -> None:
    httpx_mock.add_response(
        url="https://api.moabits.test/api/company/simListDetail/ACME",
        json=_details_payload(),
    )
    httpx_mock.add_response(
        url="https://api.moabits.test/api/company/simList/ACME",
        json=_service_status_payload(),
    )

    subs, next_cursor = await MoabitsAdapter().list_subscriptions(
        moabits_creds,
        cursor=None,
        limit=50,
        filters=SubscriptionSearchFilters(
            modified_since=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
            modified_till=datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC),
        ),
    )

    assert len(subs) == 1
    assert subs[0].iccid == "8934070100000000001"
    assert next_cursor is None


@pytest.mark.asyncio
async def test_list_subscriptions_rejects_non_date_filters(moabits_creds: dict) -> None:
    with pytest.raises(UnsupportedOperation):
        await MoabitsAdapter().list_subscriptions(
            moabits_creds,
            cursor=None,
            limit=50,
            filters=SubscriptionSearchFilters(iccid="8934070100000000001"),
        )
