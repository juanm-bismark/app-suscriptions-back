"""Unit tests for the Kite provider adapter."""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import httpx
import pytest

from app.providers.kite import client as kite_client_mod
from app.providers.kite.adapter import KiteAdapter
from app.shared.errors import UnsupportedOperation
from app.subscriptions.domain import (
    AdministrativeStatus,
    ConnectivityState,
    StatusDetail,
    StatusHistoryRecord,
)


class TestKiteStatusMapping:
    def test_kite_to_canonical_all_states(self):
        from app.providers.kite.status_map import map_status

        assert map_status("ACTIVE") == AdministrativeStatus.ACTIVE
        assert map_status("TEST") == AdministrativeStatus.IN_TEST
        assert map_status("INACTIVE_NEW") == AdministrativeStatus.INACTIVE_NEW
        assert map_status("ACTIVATION_READY") == AdministrativeStatus.ACTIVATION_READY
        assert (
            map_status("ACTIVATION_PENDANT") == AdministrativeStatus.ACTIVATION_PENDANT
        )
        assert map_status("DEACTIVATED") == AdministrativeStatus.TERMINATED
        assert map_status("SUSPENDED") == AdministrativeStatus.SUSPENDED
        assert map_status("RETIRED") == AdministrativeStatus.RETIRED
        assert map_status("RESTORE") == AdministrativeStatus.RESTORE
        assert map_status("PENDING") == AdministrativeStatus.PENDING

    def test_kite_unknown_status_falls_back(self):
        from app.providers.kite.status_map import map_status

        assert map_status("UNKNOWN_STATE") == AdministrativeStatus.UNKNOWN
        assert map_status("") == AdministrativeStatus.UNKNOWN

    def test_canonical_to_kite_all_states(self):
        from app.providers.kite.status_map import to_native

        assert to_native(AdministrativeStatus.ACTIVE) == "ACTIVE"
        assert to_native(AdministrativeStatus.IN_TEST) == "TEST"
        assert to_native(AdministrativeStatus.INACTIVE_NEW) == "INACTIVE_NEW"
        assert to_native(AdministrativeStatus.ACTIVATION_READY) == "ACTIVATION_READY"
        assert (
            to_native(AdministrativeStatus.ACTIVATION_PENDANT) == "ACTIVATION_PENDANT"
        )

    def test_kite_reverse_mapping_unsupported_states(self):
        from app.providers.kite.status_map import to_native

        assert to_native(AdministrativeStatus.PENDING) is None
        assert to_native(AdministrativeStatus.PURGED) is None
        assert to_native(AdministrativeStatus.TERMINATED) is None
        assert to_native(AdministrativeStatus.SUSPENDED) is None


@pytest.fixture(autouse=True)
def _clear_kite_transport_state() -> None:
    kite_client_mod._REQUEST_SEMAPHORES.clear()
    kite_client_mod._SSL_CONTEXT_CACHE.clear()


class TestKiteAdapterBehavior:
    @staticmethod
    def _subscription_response() -> str:
        return """
        <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
            xmlns:gm2minve_s3t="http://www.telefonica.com/schemas/UNICA/SOAP/Globalm2m/inventory/v12/types">
          <soapenv:Body>
            <gm2minve_s3t:getSubscriptionDetailResponse>
              <gm2minve_s3t:subscriptionDetailData>
                <gm2minve_s3t:icc>8934070100000000001</gm2minve_s3t:icc>
                <gm2minve_s3t:imsi>214070000000001</gm2minve_s3t:imsi>
                <gm2minve_s3t:msisdn>346000000001</gm2minve_s3t:msisdn>
                <gm2minve_s3t:alias>Tracker SIM</gm2minve_s3t:alias>
                <gm2minve_s3t:lifeCycleStatus>ACTIVE</gm2minve_s3t:lifeCycleStatus>
                <gm2minve_s3t:activationDate>2024-08-02T00:00:00Z</gm2minve_s3t:activationDate>
                <gm2minve_s3t:lastStateChangeDate>2024-08-04T00:00:00Z</gm2minve_s3t:lastStateChangeDate>
                <gm2minve_s3t:sgsnIP>192.0.2.10</gm2minve_s3t:sgsnIP>
                <gm2minve_s3t:ggsnIP>192.0.2.11</gm2minve_s3t:ggsnIP>
                <gm2minve_s3t:manualLocation>
                  <gm2minve_s3t:coordinates>
                    <gm2minve_s3t:latitude>40.4168</gm2minve_s3t:latitude>
                    <gm2minve_s3t:longitude>-3.7038</gm2minve_s3t:longitude>
                  </gm2minve_s3t:coordinates>
                </gm2minve_s3t:manualLocation>
                <gm2minve_s3t:basicServices>
                  <gm2minve_s3t:voiceOriginatedHome>true</gm2minve_s3t:voiceOriginatedHome>
                  <gm2minve_s3t:dataHome>true</gm2minve_s3t:dataHome>
                </gm2minve_s3t:basicServices>
                <gm2minve_s3t:supplServices>
                  <gm2minve_s3t:vpn>true</gm2minve_s3t:vpn>
                  <gm2minve_s3t:dim>false</gm2minve_s3t:dim>
                </gm2minve_s3t:supplServices>
                <gm2minve_s3t:consumptionDaily>
                  <gm2minve_s3t:voice>
                    <gm2minve_s3t:limit>100</gm2minve_s3t:limit>
                    <gm2minve_s3t:value>12</gm2minve_s3t:value>
                    <gm2minve_s3t:thrReached>0</gm2minve_s3t:thrReached>
                    <gm2minve_s3t:enabled>true</gm2minve_s3t:enabled>
                    <gm2minve_s3t:trafficCut>false</gm2minve_s3t:trafficCut>
                  </gm2minve_s3t:voice>
                  <gm2minve_s3t:sms>
                    <gm2minve_s3t:limit>50</gm2minve_s3t:limit>
                    <gm2minve_s3t:value>5</gm2minve_s3t:value>
                    <gm2minve_s3t:thrReached>0</gm2minve_s3t:thrReached>
                    <gm2minve_s3t:enabled>true</gm2minve_s3t:enabled>
                    <gm2minve_s3t:trafficCut>false</gm2minve_s3t:trafficCut>
                  </gm2minve_s3t:sms>
                  <gm2minve_s3t:data>
                    <gm2minve_s3t:limit>1000</gm2minve_s3t:limit>
                    <gm2minve_s3t:value>256</gm2minve_s3t:value>
                    <gm2minve_s3t:thrReached>0</gm2minve_s3t:thrReached>
                    <gm2minve_s3t:enabled>true</gm2minve_s3t:enabled>
                    <gm2minve_s3t:trafficCut>false</gm2minve_s3t:trafficCut>
                  </gm2minve_s3t:data>
                </gm2minve_s3t:consumptionDaily>
                <gm2minve_s3t:consumptionMonthly>
                  <gm2minve_s3t:voice>
                    <gm2minve_s3t:limit>1000</gm2minve_s3t:limit>
                    <gm2minve_s3t:value>120</gm2minve_s3t:value>
                    <gm2minve_s3t:thrReached>0</gm2minve_s3t:thrReached>
                    <gm2minve_s3t:enabled>true</gm2minve_s3t:enabled>
                    <gm2minve_s3t:trafficCut>false</gm2minve_s3t:trafficCut>
                  </gm2minve_s3t:voice>
                  <gm2minve_s3t:sms>
                    <gm2minve_s3t:limit>500</gm2minve_s3t:limit>
                    <gm2minve_s3t:value>45</gm2minve_s3t:value>
                    <gm2minve_s3t:thrReached>0</gm2minve_s3t:thrReached>
                    <gm2minve_s3t:enabled>true</gm2minve_s3t:enabled>
                    <gm2minve_s3t:trafficCut>false</gm2minve_s3t:trafficCut>
                  </gm2minve_s3t:sms>
                  <gm2minve_s3t:data>
                    <gm2minve_s3t:limit>2048</gm2minve_s3t:limit>
                    <gm2minve_s3t:value>768</gm2minve_s3t:value>
                    <gm2minve_s3t:thrReached>0</gm2minve_s3t:thrReached>
                    <gm2minve_s3t:enabled>true</gm2minve_s3t:enabled>
                    <gm2minve_s3t:trafficCut>false</gm2minve_s3t:trafficCut>
                  </gm2minve_s3t:data>
                </gm2minve_s3t:consumptionMonthly>
              </gm2minve_s3t:subscriptionDetailData>
            </gm2minve_s3t:getSubscriptionDetailResponse>
          </soapenv:Body>
        </soapenv:Envelope>
        """

    @staticmethod
    def _presence_response() -> str:
        return """
        <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
            xmlns:gm2minve_s3t="http://www.telefonica.com/schemas/UNICA/SOAP/Globalm2m/inventory/v12/types">
          <soapenv:Body>
            <gm2minve_s3t:getPresenceDetailResponse>
              <gm2minve_s3t:presenceDetailData>
                <gm2minve_s3t:level>IP</gm2minve_s3t:level>
                <gm2minve_s3t:timeStamp>2026-04-29T12:34:56Z</gm2minve_s3t:timeStamp>
                <gm2minve_s3t:ip>10.1.2.3</gm2minve_s3t:ip>
                <gm2minve_s3t:ratType>7</gm2minve_s3t:ratType>
              </gm2minve_s3t:presenceDetailData>
            </gm2minve_s3t:getPresenceDetailResponse>
          </soapenv:Body>
        </soapenv:Envelope>
        """

    @pytest.mark.asyncio
    async def test_list_subscriptions_sends_limit_as_batch_size(
        self, monkeypatch: pytest.MonkeyPatch, kite_creds: dict[str, str]
    ) -> None:
        captured = {}

        async def fake_get_subscriptions(
            self,
            *,
            start_index: int | None = None,
            batch_size: int | None = None,
            searchParameters: dict[str, str] | None = None,
            maxBatchSize: int | None = None,
        ) -> ET.Element:
            captured["start_index"] = start_index
            captured["batch_size"] = batch_size
            captured["searchParameters"] = searchParameters
            captured["maxBatchSize"] = maxBatchSize
            return ET.fromstring("<root />")

        monkeypatch.setattr(
            kite_client_mod.KiteClient,
            "get_subscriptions",
            fake_get_subscriptions,
        )

        subs, next_cursor = await KiteAdapter().list_subscriptions(
            kite_creds,
            cursor="5",
            limit=1,
        )

        assert subs == []
        assert next_cursor is None
        assert captured == {
            "start_index": 5,
            "batch_size": 1,
            "searchParameters": None,
            "maxBatchSize": None,
        }

    @pytest.mark.asyncio
    async def test_kite_client_uses_configured_timeout(
        self, monkeypatch: pytest.MonkeyPatch, kite_creds: dict[str, str]
    ) -> None:
        monkeypatch.setattr(
            kite_client_mod,
            "get_settings",
            lambda: SimpleNamespace(
                kite_request_timeout_seconds=6.5,
                kite_max_concurrent_requests=10,
            ),
        )
        seen_timeout = None

        async def fake_post(
            client: httpx.AsyncClient,
            _url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            nonlocal seen_timeout
            seen_timeout = client.timeout.read
            return httpx.Response(
                200, text=TestKiteAdapterBehavior._presence_response()
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        await kite_client_mod.KiteClient(kite_creds).get_presence_detail(
            "8934070100000000001"
        )

        assert seen_timeout == 6.5

    @pytest.mark.asyncio
    async def test_kite_requests_are_limited_by_semaphore(
        self, monkeypatch: pytest.MonkeyPatch, kite_creds: dict[str, str]
    ) -> None:
        monkeypatch.setattr(
            kite_client_mod,
            "get_settings",
            lambda: SimpleNamespace(
                kite_request_timeout_seconds=30.0,
                kite_max_concurrent_requests=2,
            ),
        )

        in_flight = 0
        peak = 0
        lock = asyncio.Lock()

        async def fake_post(
            _client: httpx.AsyncClient,
            _url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            nonlocal in_flight, peak
            async with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            async with lock:
                in_flight -= 1
            return httpx.Response(
                200, text=TestKiteAdapterBehavior._presence_response()
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        client = kite_client_mod.KiteClient(kite_creds)

        await asyncio.gather(
            *(client.get_presence_detail(f"893407010000000000{i}") for i in range(6))
        )

        assert peak == 2

    def test_kite_ssl_context_is_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        created = 0

        class FakeContext:
            def load_verify_locations(self, cadata: str | None = None) -> None:
                assert cadata == "test-ca"

        def fake_create_default_context(cafile: str | None = None) -> FakeContext:
            nonlocal created
            created += 1
            return FakeContext()

        monkeypatch.setattr(kite_client_mod.certifi, "where", lambda: "certifi.pem")
        monkeypatch.setattr(
            kite_client_mod.ssl,
            "create_default_context",
            fake_create_default_context,
        )
        creds = kite_client_mod._creds(
            {"endpoint": "https://kite.test/soap", "server_ca_bundle_pem": "test-ca"}
        )

        first = kite_client_mod._ssl_context(creds)
        second = kite_client_mod._ssl_context(creds)

        assert first is second
        assert created == 1

    @pytest.mark.asyncio
    async def test_get_subscription_parses_soap(
        self, monkeypatch: pytest.MonkeyPatch, kite_creds: dict[str, str]
    ) -> None:
        async def fake_post(
            _client: httpx.AsyncClient,
            url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            assert url == kite_creds["endpoint"]
            assert headers is not None
            assert headers["SOAPAction"] == '"urn:getSubscriptionDetail"'
            assert content is not None
            assert b"<gm2minve_s3t:getSubscriptionDetail>" in content
            assert (
                b'xmlns:gm2minve_s3t="http://www.telefonica.com/schemas/UNICA/SOAP/Globalm2m/inventory/v12/types"'
                in content
            )
            return httpx.Response(
                200, text=TestKiteAdapterBehavior._subscription_response()
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        sub = await KiteAdapter().get_subscription("8934070100000000001", kite_creds)

        assert sub.iccid == "8934070100000000001"
        assert sub.status == AdministrativeStatus.ACTIVE
        assert sub.provider_fields["sgsn_ip"] == "192.0.2.10"
        assert sub.provider_fields["ggsn_ip"] == "192.0.2.11"
        assert sub.provider_fields["manual_location"] == {
            "lat": "40.4168",
            "lng": "-3.7038",
        }
        assert sub.provider_fields["basic_services"]["voiceOriginatedHome"] is True
        assert sub.provider_fields["supplementary_services"] == ["vpn"]
        assert sub.provider_fields["consumption_monthly"]["data"]["value"] == "768"

    @pytest.mark.asyncio
    async def test_get_usage_extracts_consumption(
        self, monkeypatch: pytest.MonkeyPatch, kite_creds: dict[str, str]
    ) -> None:
        async def fake_post(
            _client: httpx.AsyncClient,
            _url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            return httpx.Response(
                200, text=TestKiteAdapterBehavior._subscription_response()
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        snap = await KiteAdapter().get_usage("8934070100000000001", kite_creds)

        assert snap.data_used_bytes == 768
        assert snap.sms_count == 45
        assert snap.voice_seconds == 120
        assert len(snap.usage_metrics) == 6

    @pytest.mark.asyncio
    async def test_get_presence_parses_known_level(
        self, monkeypatch: pytest.MonkeyPatch, kite_creds: dict[str, str]
    ) -> None:
        async def fake_post(
            _client: httpx.AsyncClient,
            _url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            return httpx.Response(
                200, text=TestKiteAdapterBehavior._presence_response()
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        presence = await KiteAdapter().get_presence("8934070100000000001", kite_creds)

        assert presence.state == ConnectivityState.ONLINE
        assert presence.ip_address == "10.1.2.3"
        assert presence.rat_type == "7"

    @pytest.mark.asyncio
    async def test_get_presence_gprs_and_ip_reachability_online(
        self, monkeypatch: pytest.MonkeyPatch, kite_creds: dict[str, str]
    ) -> None:
        async def fake_post_gprs(
            _client: httpx.AsyncClient,
            _url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            body = """
            <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                xmlns:gm2minve_s3t="http://www.telefonica.com/schemas/UNICA/SOAP/Globalm2m/inventory/v12/types">
              <soapenv:Body>
                <gm2minve_s3t:getPresenceDetailResponse>
                  <gm2minve_s3t:presenceDetailData>
                    <gm2minve_s3t:level>GPRS</gm2minve_s3t:level>
                    <gm2minve_s3t:timeStamp>2026-04-29T12:34:56Z</gm2minve_s3t:timeStamp>
                    <gm2minve_s3t:ip>10.1.2.4</gm2minve_s3t:ip>
                  </gm2minve_s3t:presenceDetailData>
                </gm2minve_s3t:getPresenceDetailResponse>
              </soapenv:Body>
            </soapenv:Envelope>
            """
            return httpx.Response(200, text=body)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post_gprs)
        presence_gprs = await KiteAdapter().get_presence(
            "8934070100000000002", kite_creds
        )
        assert presence_gprs.state == ConnectivityState.ONLINE

        async def fake_post_ipreach(
            _client: httpx.AsyncClient,
            _url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            body = """
            <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                xmlns:gm2minve_s3t="http://www.telefonica.com/schemas/UNICA/SOAP/Globalm2m/inventory/v12/types">
              <soapenv:Body>
                <gm2minve_s3t:getPresenceDetailResponse>
                  <gm2minve_s3t:presenceDetailData>
                    <gm2minve_s3t:level>IP reachability</gm2minve_s3t:level>
                    <gm2minve_s3t:timeStamp>2026-04-29T12:34:56Z</gm2minve_s3t:timeStamp>
                    <gm2minve_s3t:ip>10.1.2.5</gm2minve_s3t:ip>
                  </gm2minve_s3t:presenceDetailData>
                </gm2minve_s3t:getPresenceDetailResponse>
              </soapenv:Body>
            </soapenv:Envelope>
            """
            return httpx.Response(200, text=body)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post_ipreach)
        presence_ip = await KiteAdapter().get_presence(
            "8934070100000000003", kite_creds
        )
        assert presence_ip.state == ConnectivityState.ONLINE

    @pytest.mark.asyncio
    async def test_get_presence_gsm_offline_and_unknown(
        self, monkeypatch: pytest.MonkeyPatch, kite_creds: dict[str, str]
    ) -> None:
        async def fake_post_gsm(
            _client: httpx.AsyncClient,
            _url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            body = """
            <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                xmlns:gm2minve_s3t="http://www.telefonica.com/schemas/UNICA/SOAP/Globalm2m/inventory/v12/types">
              <soapenv:Body>
                <gm2minve_s3t:getPresenceDetailResponse>
                  <gm2minve_s3t:presenceDetailData>
                    <gm2minve_s3t:level>GSM</gm2minve_s3t:level>
                    <gm2minve_s3t:timeStamp>2026-04-29T12:34:56Z</gm2minve_s3t:timeStamp>
                  </gm2minve_s3t:presenceDetailData>
                </gm2minve_s3t:getPresenceDetailResponse>
              </soapenv:Body>
            </soapenv:Envelope>
            """
            return httpx.Response(200, text=body)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post_gsm)
        presence_gsm = await KiteAdapter().get_presence(
            "8934070100000000004", kite_creds
        )
        assert presence_gsm.state == ConnectivityState.OFFLINE

        async def fake_post_unknown(
            _client: httpx.AsyncClient,
            _url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            body = """
            <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                xmlns:gm2minve_s3t="http://www.telefonica.com/schemas/UNICA/SOAP/Globalm2m/inventory/v12/types">
              <soapenv:Body>
                <gm2minve_s3t:getPresenceDetailResponse>
                  <gm2minve_s3t:presenceDetailData>
                    <gm2minve_s3t:level>unknown</gm2minve_s3t:level>
                  </gm2minve_s3t:presenceDetailData>
                </gm2minve_s3t:getPresenceDetailResponse>
              </soapenv:Body>
            </soapenv:Envelope>
            """
            return httpx.Response(200, text=body)

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post_unknown)
        presence_unknown = await KiteAdapter().get_presence(
            "8934070100000000005", kite_creds
        )
        assert presence_unknown.state == ConnectivityState.UNKNOWN

    @pytest.mark.asyncio
    async def test_set_administrative_status_unsupported(self) -> None:
        with pytest.raises(UnsupportedOperation):
            await KiteAdapter().set_administrative_status(
                "8934070100000000001",
                {"company_id": "company-1"},
                target=AdministrativeStatus.ACTIVE,
                idempotency_key="idem-1",
            )

    @staticmethod
    def _status_detail_response() -> str:
        """Fixture: subscription in ACTIVE state, manually changed 3 days ago."""
        return """
        <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
            xmlns:gm2minve_s3t="http://www.telefonica.com/schemas/UNICA/SOAP/Globalm2m/inventory/v12/types">
          <soapenv:Body>
            <gm2minve_s3t:getStatusDetailResponse>
              <gm2minve_s3t:statusDetailData>
                <gm2minve_s3t:state>ACTIVE</gm2minve_s3t:state>
                <gm2minve_s3t:automatic>false</gm2minve_s3t:automatic>
                <gm2minve_s3t:currentStatusDate>2026-04-27T14:32:00Z</gm2minve_s3t:currentStatusDate>
                <gm2minve_s3t:changeReason>Manual activation via portal</gm2minve_s3t:changeReason>
                <gm2minve_s3t:user>admin@company.com</gm2minve_s3t:user>
              </gm2minve_s3t:statusDetailData>
            </gm2minve_s3t:getStatusDetailResponse>
          </soapenv:Body>
        </soapenv:Envelope>
        """

    @staticmethod
    def _status_detail_suspended_response() -> str:
        """Fixture: subscription in SUSPENDED state, suspended automatically due to overuse."""
        return """
        <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
            xmlns:gm2minve_s3t="http://www.telefonica.com/schemas/UNICA/SOAP/Globalm2m/inventory/v12/types">
          <soapenv:Body>
            <gm2minve_s3t:getStatusDetailResponse>
              <gm2minve_s3t:statusDetailData>
                <gm2minve_s3t:state>SUSPENDED</gm2minve_s3t:state>
                <gm2minve_s3t:automatic>true</gm2minve_s3t:automatic>
                <gm2minve_s3t:currentStatusDate>2026-04-20T09:15:30Z</gm2minve_s3t:currentStatusDate>
                <gm2minve_s3t:changeReason>Data quota exceeded</gm2minve_s3t:changeReason>
              </gm2minve_s3t:statusDetailData>
            </gm2minve_s3t:getStatusDetailResponse>
          </soapenv:Body>
        </soapenv:Envelope>
        """

    @staticmethod
    def _status_history_response() -> str:
        """Fixture: subscription status history with 4 transitions over a month."""
        return """
        <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
            xmlns:gm2minve_s3t="http://www.telefonica.com/schemas/UNICA/SOAP/Globalm2m/inventory/v12/types">
          <soapenv:Body>
            <gm2minve_s3t:getStatusHistoryResponse>
              <gm2minve_s3t:statusHistoryData>
                <gm2minve_s3t:state>PENDING</gm2minve_s3t:state>
                <gm2minve_s3t:automatic>false</gm2minve_s3t:automatic>
                <gm2minve_s3t:time>2026-03-30T10:00:00Z</gm2minve_s3t:time>
                <gm2minve_s3t:reason>SIM card registered</gm2minve_s3t:reason>
                <gm2minve_s3t:user>provisioning@telefonica.es</gm2minve_s3t:user>
              </gm2minve_s3t:statusHistoryData>
              <gm2minve_s3t:statusHistoryData>
                <gm2minve_s3t:state>ACTIVE</gm2minve_s3t:state>
                <gm2minve_s3t:automatic>true</gm2minve_s3t:automatic>
                <gm2minve_s3t:time>2026-04-01T08:30:00Z</gm2minve_s3t:time>
                <gm2minve_s3t:reason>Automatic activation after 2 days</gm2minve_s3t:reason>
              </gm2minve_s3t:statusHistoryData>
              <gm2minve_s3t:statusHistoryData>
                <gm2minve_s3t:state>SUSPENDED</gm2minve_s3t:state>
                <gm2minve_s3t:automatic>true</gm2minve_s3t:automatic>
                <gm2minve_s3t:time>2026-04-15T11:45:00Z</gm2minve_s3t:time>
                <gm2minve_s3t:reason>Monthly quota exceeded (2GB)</gm2minve_s3t:reason>
              </gm2minve_s3t:statusHistoryData>
              <gm2minve_s3t:statusHistoryData>
                <gm2minve_s3t:state>ACTIVE</gm2minve_s3t:state>
                <gm2minve_s3t:automatic>false</gm2minve_s3t:automatic>
                <gm2minve_s3t:time>2026-04-27T14:32:00Z</gm2minve_s3t:time>
                <gm2minve_s3t:reason>Manual reactivation by customer</gm2minve_s3t:reason>
                <gm2minve_s3t:user>admin@company.com</gm2minve_s3t:user>
              </gm2minve_s3t:statusHistoryData>
            </gm2minve_s3t:getStatusHistoryResponse>
          </soapenv:Body>
        </soapenv:Envelope>
        """

    @pytest.mark.asyncio
    async def test_get_status_detail_active_manual(
        self, monkeypatch: pytest.MonkeyPatch, kite_creds: dict[str, str]
    ) -> None:
        """Test parsing getStatusDetail for an ACTIVE SIM manually changed."""

        async def fake_post(
            _client: httpx.AsyncClient,
            _url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            assert headers is not None
            assert headers["SOAPAction"] == '"urn:getStatusDetail"'
            return httpx.Response(
                200, text=TestKiteAdapterBehavior._status_detail_response()
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        status = await KiteAdapter().get_status_detail(
            "8934070100000000001", kite_creds
        )

        assert isinstance(status, StatusDetail)
        assert status.iccid == "8934070100000000001"
        assert status.state == "ACTIVE"
        assert status.automatic is False
        assert status.change_reason == "Manual activation via portal"
        assert status.user == "admin@company.com"
        assert status.current_status_date.year == 2026
        assert status.current_status_date.month == 4
        assert status.current_status_date.day == 27

    @pytest.mark.asyncio
    async def test_get_status_detail_suspended_auto(
        self, monkeypatch: pytest.MonkeyPatch, kite_creds: dict[str, str]
    ) -> None:
        """Test parsing getStatusDetail for a SUSPENDED SIM (auto-suspended)."""

        async def fake_post(
            _client: httpx.AsyncClient,
            _url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            return httpx.Response(
                200, text=TestKiteAdapterBehavior._status_detail_suspended_response()
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        status = await KiteAdapter().get_status_detail(
            "8934070100000000001", kite_creds
        )

        assert status.state == "SUSPENDED"
        assert status.automatic is True
        assert status.change_reason == "Data quota exceeded"
        assert status.user is None  # Auto actions have no user

    @pytest.mark.asyncio
    async def test_get_status_history_full_lifecycle(
        self, monkeypatch: pytest.MonkeyPatch, kite_creds: dict[str, str]
    ) -> None:
        """Test parsing getStatusHistory with a full SIM lifecycle."""

        async def fake_post(
            _client: httpx.AsyncClient,
            _url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            assert headers is not None
            assert headers["SOAPAction"] == '"urn:getStatusHistory"'
            assert content is not None
            assert b"<gm2minve_s3t:getStatusHistory>" in content
            return httpx.Response(
                200, text=TestKiteAdapterBehavior._status_history_response()
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        history = await KiteAdapter().get_status_history(
            "8934070100000000001", kite_creds
        )

        assert isinstance(history, list)
        assert len(history) == 4
        assert all(isinstance(r, StatusHistoryRecord) for r in history)

        # First event: PENDING → registration
        assert history[0].state == "PENDING"
        assert history[0].automatic is False
        assert history[0].user == "provisioning@telefonica.es"

        # Second event: ACTIVE → automatic activation
        assert history[1].state == "ACTIVE"
        assert history[1].automatic is True
        assert "automatic activation" in history[1].reason.lower()

        # Third event: SUSPENDED → quota exceeded
        assert history[2].state == "SUSPENDED"
        assert history[2].automatic is True

        # Fourth event: back to ACTIVE → manual reactivation
        assert history[3].state == "ACTIVE"
        assert history[3].automatic is False
        assert history[3].user == "admin@company.com"

    @pytest.mark.asyncio
    async def test_get_status_history_with_date_range(
        self, monkeypatch: pytest.MonkeyPatch, kite_creds: dict[str, str]
    ) -> None:
        """Test that getStatusHistory passes start_date and end_date to the client."""

        async def fake_post(
            _client: httpx.AsyncClient,
            _url: str,
            content: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            # Verify date parameters are in the SOAP body
            assert content is not None
            assert (
                b"<gm2minve_s3t:startDate>2026-04-01T00:00:00Z</gm2minve_s3t:startDate>"
                in content
            )
            assert (
                b"<gm2minve_s3t:endDate>2026-04-30T23:59:59Z</gm2minve_s3t:endDate>"
                in content
            )
            return httpx.Response(
                200, text=TestKiteAdapterBehavior._status_history_response()
            )

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        history = await KiteAdapter().get_status_history(
            "8934070100000000001",
            kite_creds,
            start_date="2026-04-01T00:00:00Z",
            end_date="2026-04-30T23:59:59Z",
        )

        assert len(history) == 4
