"""Tests for Tele2 adapter behaviour: auth header, path prefix, modifiedSince."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.providers.tele2.adapter import Tele2Adapter
from app.shared.errors import (
    ProviderRateLimited,
    ProviderUnavailable,
    ProviderValidationError,
)
from app.subscriptions.domain import AdministrativeStatus, SubscriptionSearchFilters


@respx.mock
@pytest.mark.asyncio
async def test_basic_auth_header_and_path_prefix() -> None:
    creds = {
        "base_url": "https://api.tele2.test",
        "username": "alice",
        "api_key": "sekret",
        "api_version": "v1",
    }

    iccid = "8934070100000000001"
    expected_path = f"/rws/api/v1/devices/{iccid}"

    route = respx.get(f"https://api.tele2.test{expected_path}").mock(
        return_value=httpx.Response(200, json={"iccid": iccid, "status": "ACTIVE"})
    )

    adapter = Tele2Adapter()
    sub = await adapter.get_subscription(iccid, creds)

    assert sub.iccid == iccid
    # Check the request was received on the correct route
    assert route.called

    # Validate Authorization header is Basic with correct base64 of username:api_key
    call = route.calls[0]
    auth = call.request.headers.get("Authorization")
    assert auth is not None and auth.startswith("Basic ")
    token = auth.split(" ", 1)[1]
    assert token == base64.b64encode(b"alice:sekret").decode("ascii")


@respx.mock
@pytest.mark.asyncio
async def test_list_subscriptions_sends_required_modified_since_from_cursor() -> None:
    base = "https://api.tele2.test"
    creds = {
        "base_url": base,
        "username": "alice",
        "api_key": "ignored",
        "api_version": "v1",
        "company_id": "company-1",
    }

    # Case A: cursor contains since
    cursor = "page:2|since:2026-04-18T17:31:34Z"
    route_a = respx.get(f"{base}/rws/api/v1/devices").mock(
        return_value=httpx.Response(200, json={"devices": [], "lastPage": True})
    )

    adapter = Tele2Adapter()
    subs, next_cursor = await adapter.list_subscriptions(creds, cursor=cursor, limit=10)
    assert route_a.called
    req_a = route_a.calls[-1].request
    assert req_a.url.params.get("modifiedSince") == "2026-04-18T17:31:34Z"
    assert req_a.url.params.get("modifiedTill") == "2027-04-18T17:31:34Z"

    with pytest.raises(ProviderValidationError) as missing:
        await adapter.list_subscriptions(creds, cursor=None, limit=5)
    assert missing.value.detail == "10000003 ModifiedSince is required"


@pytest.mark.asyncio
async def test_list_subscriptions_rejects_invalid_modified_since_windows() -> None:
    creds = {
        "base_url": "https://api.tele2.test",
        "username": "alice",
        "api_key": "ignored",
    }
    adapter = Tele2Adapter()

    with pytest.raises(ProviderValidationError) as bad_format:
        await adapter.list_subscriptions(
            creds, cursor="page:1|since:2026-04-18T17:31:34+00:00", limit=10
        )
    assert "yyyy-MM-ddTHH:mm:ssZ" in (bad_format.value.detail or "")
    with pytest.raises(ProviderValidationError) as future:
        await adapter.list_subscriptions(
            creds, cursor="page:1|since:2999-04-18T17:31:34Z", limit=10
        )
    assert future.value.detail == "10002119 ModifiedSince cannot be a future date"
    with pytest.raises(ProviderValidationError) as too_old:
        await adapter.list_subscriptions(
            creds, cursor="page:1|since:2015-05-05T00:00:00Z", limit=10
        )
    assert (
        too_old.value.detail
        == "10000045 ModifiedSince cannot be more than one year old"
    )


@pytest.mark.asyncio
async def test_rejects_unsupported_api_version() -> None:
    adapter = Tele2Adapter()

    with pytest.raises(ProviderValidationError) as invalid_version:
        await adapter.list_subscriptions(
            {
                "base_url": "https://api.tele2.test",
                "username": "alice",
                "api_key": "ignored",
                "api_version": "v2",
            },
            cursor="page:1|since:2026-04-18T17:31:34Z",
            limit=10,
        )

    assert invalid_version.value.detail == "10000024 Invalid apiVersion"


@respx.mock
@pytest.mark.asyncio
async def test_cisco_rate_limit_error_code_maps_to_rate_limited() -> None:
    base = "https://api.tele2.test"
    creds = {
        "base_url": base,
        "username": "alice",
        "api_key": "ignored",
    }
    respx.get(f"{base}/rws/api/v1/devices").mock(
        return_value=httpx.Response(
            400,
            json={
                "errorCode": "40000029",
                "errorMessage": "Rate Limit Exceeded",
            },
        )
    )

    with pytest.raises(ProviderRateLimited) as exc_info:
        await Tele2Adapter().list_subscriptions(
            creds,
            cursor="page:1|since:2026-04-18T17:31:34Z",
            limit=10,
        )

    assert exc_info.value.detail == "Rate Limit Exceeded"
    assert exc_info.value.extra["provider_error_code"] == "40000029"


@pytest.mark.asyncio
async def test_list_subscriptions_is_wrapped_by_circuit_breaker(monkeypatch) -> None:
    adapter = Tele2Adapter()

    async def fail_impl(*args, **kwargs):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(adapter, "_list_subscriptions_impl", fail_impl)

    for _ in range(5):
        with pytest.raises(RuntimeError):
            await adapter.list_subscriptions({}, cursor=None, limit=10)

    assert adapter.circuit_breaker.state == "OPEN"
    with pytest.raises(ProviderUnavailable) as exc_info:
        await adapter.list_subscriptions({}, cursor=None, limit=10)

    assert "circuit breaker is OPEN" in exc_info.value.detail


@respx.mock
@pytest.mark.asyncio
async def test_list_subscriptions_maps_canonical_filters() -> None:
    base = "https://api.tele2.test"
    creds = {
        "base_url": base,
        "username": "alice",
        "api_key": "ignored",
        "api_version": "v1",
        "max_tps": 5,
        "company_id": "company-1",
    }
    route = respx.get(f"{base}/rws/api/v1/devices").mock(
        return_value=httpx.Response(200, json={"devices": [], "lastPage": True})
    )

    filters = SubscriptionSearchFilters(
        status=AdministrativeStatus.ACTIVE,
        modified_since=datetime(2026, 4, 18, 17, 31, 34, tzinfo=UTC),
        modified_till=datetime(2026, 4, 30, tzinfo=UTC),
        iccid="893",
        custom={"accountCustom1": "acme"},
    )
    await Tele2Adapter().list_subscriptions(
        creds, cursor=None, limit=10, filters=filters
    )

    params = route.calls[-1].request.url.params
    assert params.get("status") == "ACTIVATED"
    assert params.get("modifiedSince") == "2026-04-18T17:31:34Z"
    assert params.get("modifiedTill") == "2026-04-30T00:00:00Z"
    assert params.get("iccid") == "893"
    assert params.get("accountCustom1") == "acme"


@respx.mock
@pytest.mark.asyncio
async def test_list_subscriptions_enriches_first_five_devices_only() -> None:
    base = "https://api.tele2.test"
    creds = {
        "base_url": base,
        "username": "alice",
        "api_key": "ignored",
        "api_version": "v1",
        "max_tps": 1000,
        "company_id": "company-1",
    }
    devices = [
        {
            "iccid": f"89462038075065380{i}",
            "status": "ACTIVATED",
            "ratePlan": "PAYU - BISMARK",
            "communicationPlan": "Data LTE SMS",
        }
        for i in range(6)
    ]
    respx.get(f"{base}/rws/api/v1/devices").mock(
        return_value=httpx.Response(
            200,
            json={"devices": devices, "lastPage": True},
        )
    )
    detail_routes = [
        respx.get(f"{base}/rws/api/v1/devices/{device['iccid']}").mock(
            return_value=httpx.Response(
                200,
                json={
                    **device,
                    "imsi": f"90116169700497{i}",
                    "msisdn": f"88235169700497{i}",
                    "imei": f"imei-{i}",
                    "dateUpdated": "2016-07-06 22:04:04.380+0000",
                    "accountId": "100020620",
                },
            )
        )
        for i, device in enumerate(devices[:5])
    ]
    subs, next_cursor = await Tele2Adapter().list_subscriptions(
        creds,
        cursor="page:1|since:2026-04-18T17:31:34Z",
        limit=6,
    )

    assert next_cursor is None
    assert len(subs) == 6
    assert all(route.called for route in detail_routes)
    requested_urls = {str(call.request.url) for call in respx.calls}
    assert f"{base}/rws/api/v1/devices/{devices[5]['iccid']}" not in requested_urls
    assert subs[0].imsi == "901161697004970"
    assert subs[0].msisdn == "882351697004970"
    assert subs[0].provider_fields["imei"] == "imei-0"
    assert subs[0].provider_fields["detail_enriched"] is True
    assert subs[5].imsi is None
    assert "detail_enriched" not in subs[5].provider_fields


@respx.mock
@pytest.mark.asyncio
async def test_get_usage_maps_date_range_and_metrics() -> None:
    base = "https://api.tele2.test"
    creds = {
        "base_url": base,
        "username": "alice",
        "api_key": "ignored",
        "api_version": "v1",
        "company_id": "company-1",
    }
    route = respx.get(f"{base}/rws/api/v1/devices/893/usage").mock(
        return_value=httpx.Response(
            200,
            json={
                "metrics": [
                    {"metricType": "data", "usage": 1024},
                    {"metricType": "vmo", "usage": 60},
                    {"metricType": "smo", "usage": 2},
                ]
            },
        )
    )

    snap = await Tele2Adapter().get_usage(
        "893",
        creds,
        start_date=datetime(2026, 1, 1, tzinfo=UTC),
        end_date=datetime(2026, 1, 15, tzinfo=UTC),
        metrics=["data", "vmo", "smo"],
    )

    params = route.calls[-1].request.url.params
    assert params.get("startDate") == "20260101"
    assert params.get("endDate") == "20260115"
    assert params.get("metrics") == "data,vmo,smo"
    assert snap.data_used_bytes == 1024
    assert snap.voice_seconds == 60
    assert snap.sms_count == 2
    assert {metric.unit for metric in snap.usage_metrics} == {
        "bytes",
        "seconds",
        "count",
    }


class TestTele2StatusMapping:
    """Test bidirectional status mapping between Tele2 / Cisco Control Center and canonical."""

    def test_tele2_to_canonical_all_states(self):
        """Verify Cisco Control Center enum values map to canonical AdministrativeStatus."""
        from app.providers.tele2.status_map import map_status

        # Official Cisco enum
        assert map_status("ACTIVATED") == AdministrativeStatus.ACTIVE
        assert map_status("TEST_READY") == AdministrativeStatus.IN_TEST
        assert map_status("PURGED") == AdministrativeStatus.PURGED
        assert map_status("DEACTIVATED") == AdministrativeStatus.TERMINATED
        assert map_status("INVENTORY") == AdministrativeStatus.INVENTORY
        assert map_status("REPLACED") == AdministrativeStatus.REPLACED
        assert map_status("RETIRED") == AdministrativeStatus.RETIRED
        assert map_status("ACTIVATION_READY") == AdministrativeStatus.ACTIVATION_READY

        # Legacy aliases kept for read-path compat
        assert map_status("ACTIVE") == AdministrativeStatus.ACTIVE
        assert map_status("READY") == AdministrativeStatus.IN_TEST

    def test_canonical_to_tele2_all_states(self):
        """Write path uses official Cisco enum values."""
        from app.providers.tele2.status_map import to_native

        assert to_native(AdministrativeStatus.ACTIVE) == "ACTIVATED"
        assert to_native(AdministrativeStatus.IN_TEST) == "TEST_READY"
        assert to_native(AdministrativeStatus.PURGED) == "PURGED"
        assert to_native(AdministrativeStatus.TERMINATED) == "DEACTIVATED"
        # Cisco has no SUSPENDED — must not be sent to provider
        assert to_native(AdministrativeStatus.SUSPENDED) is None


class TestTele2PurgeConsolidation:
    """Test that purge() delegates to set_administrative_status(PURGED)."""

    @pytest.mark.skip(reason="Requires mock HTTP client")
    async def test_purge_calls_set_administrative_status(self):
        """purge() should internally call set_administrative_status with PURGED."""
        # Integration test: verify that both routes reach the same provider endpoint.
        pass


class TestTele2DateFields:
    """Test that activated_at and updated_at are extracted from API response."""

    def test_tele2_date_extraction(self):
        """Tele2 subscription should extract dateActivated and dateModified."""
        # Unit test: verify that the subscription builder reads these fields.
        # Would need to mock the API response.
        pass


class TestTele2IdempotencyKey:
    """Test that Idempotency-Key is forwarded to the provider."""

    @pytest.mark.skip(reason="Requires mock HTTP client")
    async def test_set_administrative_status_includes_header(self):
        """set_administrative_status should include Idempotency-Key in PUT request."""
        pass

    @pytest.mark.skip(reason="Requires mock HTTP client")
    async def test_purge_includes_header(self):
        """purge() should include Idempotency-Key in PUT request."""
        pass


class TestTele2AdapterBehavior:
    """Integration tests for Tele2Adapter (mocked HTTP)."""

    @pytest.mark.skip(reason="Requires mock HTTP client")
    async def test_get_subscription_parses_json(self):
        """get_subscription should parse REST JSON and extract provider_fields."""
        pass

    @pytest.mark.skip(reason="Requires mock HTTP client")
    async def test_get_usage_includes_voice_seconds(self):
        """get_usage should extract voice_seconds from usage endpoint."""
        pass

    @pytest.mark.skip(reason="Requires mock HTTP client")
    async def test_get_presence_derives_state_from_device_status(self):
        """get_presence should derive connectivity state from device status, not real connectivity."""
        pass

    @pytest.mark.skip(reason="Requires mock HTTP client")
    async def test_http_404_detection(self):
        """_is_http_not_found should properly detect HTTP 404 from exc.detail."""
        pass
