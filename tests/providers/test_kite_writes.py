"""Unit tests for Kite write operations (modify_subscription and adapter guard)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from base64 import b64encode
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.providers.kite import adapter as kite_adapter_mod, client as kite_client_mod
from app.providers.kite.adapter import KiteAdapter
from app.providers.kite.client import KiteClient
from app.shared.errors import ProviderAuthFailed, UnsupportedOperation
from app.subscriptions.domain import AdministrativeStatus


def test_kite_cert_only_credentials_omit_wsse_username_token():
    creds = kite_client_mod._creds(
        {
            "endpoint": "https://kite.test/soap",
            "client_cert_pfx_b64": "base64-pfx-placeholder",
        }
    )

    envelope = kite_client_mod._envelope(creds, "<gm2minve_s3t:getSubscriptions/>")

    assert b"UsernameToken" not in envelope
    assert b"<wsse:Username>" not in envelope
    assert b"<soapenv:Header>" in envelope
    assert b"<com:SOATransactionID>" in envelope
    assert b"<com:SOAConsumerTransactionID>" in envelope
    assert b"<soapenv:Body><gm2minve_s3t:getSubscriptions/></soapenv:Body>" in envelope


def test_kite_search_datetime_uses_utc_z_suffix():
    value = datetime(2026, 1, 1, 1, 1, 1, tzinfo=UTC)

    assert kite_adapter_mod._format_search_dt(value) == "2026-01-01T01:01:01Z"


def test_kite_wsse_credentials_keep_traceability_headers():
    creds = kite_client_mod._creds(
        {
            "endpoint": "https://kite.test/soap",
            "username": "u",
            "password": "p",
        }
    )

    envelope = kite_client_mod._envelope(creds, "<gm2minve_s3t:getSubscriptions/>")

    assert b"<com:SOATransactionID>" in envelope
    assert b"<com:SOAConsumerTransactionID>" in envelope
    assert b"<wsse:Username>u</wsse:Username>" in envelope
    assert envelope.index(b"<com:SOATransactionID>") < envelope.index(
        b"<wsse:Security>"
    )


def test_kite_partial_wsse_credentials_are_rejected():
    with pytest.raises(ProviderAuthFailed):
        kite_client_mod._creds(
            {
                "endpoint": "https://kite.test/soap",
                "username": "u",
            }
        )


def test_kite_server_ca_bundle_alias_is_decoded_into_tls_context(monkeypatch):
    pem = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"

    class FakeContext:
        def __init__(self):
            self.cadata = []

        def load_verify_locations(self, *, cadata):
            self.cadata.append(cadata)

    context = FakeContext()
    monkeypatch.setattr(
        kite_client_mod.ssl, "create_default_context", lambda **_kwargs: context
    )

    creds = kite_client_mod._creds(
        {
            "endpoint": "https://kite.test/soap",
            "server_ca_cert_pem_b64": b64encode(pem.encode("utf-8")).decode("ascii"),
        }
    )

    assert kite_client_mod._ssl_context(creds) is context
    assert context.cadata == [pem]


@pytest.mark.asyncio
async def test_get_subscriptions_search_body_uses_wsdl_order(monkeypatch):
    captured = {}

    async def fake_call(creds, operation, body_xml):
        captured["operation"] = operation
        captured["body"] = body_xml
        return ET.fromstring("<ok/>")

    monkeypatch.setattr(kite_client_mod, "_call", fake_call)

    client = KiteClient({"endpoint": "https://kite.test/soap"})
    await client.get_subscriptions(
        start_index=0,
        searchParameters={"icc": "8934070100000000001"},
        maxBatchSize=1,
    )

    body = captured["body"]
    assert captured["operation"] == "getSubscriptions"
    assert body.index("maxBatchSize") < body.index("startIndex")
    assert body.index("startIndex") < body.index("searchParameters")
    assert "<com:param>" in body
    assert "<com:name>icc</com:name>" in body
    assert "<com:value>8934070100000000001</com:value>" in body
    assert "<gm2minve_s3t:param>" not in body


@pytest.mark.asyncio
async def test_kite_usage_rejects_historical_window(kite_creds):
    with pytest.raises(UnsupportedOperation):
        await KiteAdapter().get_usage(
            "8934070100000000001",
            kite_creds,
            start_date=datetime(2026, 1, 1, tzinfo=UTC),
            end_date=datetime(2026, 1, 2, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_modify_subscription_calls_call_with_expected_operation_and_body(
    monkeypatch,
):
    captured = {}

    async def fake_call(creds, operation, body_xml):
        captured["creds"] = creds
        captured["operation"] = operation
        captured["body"] = body_xml
        return ET.fromstring("<ok/>")

    monkeypatch.setattr(kite_client_mod, "_call", fake_call)

    creds = {"endpoint": "https://kite.test/soap", "username": "u", "password": "p"}
    client = KiteClient(creds)
    await client.modify_subscription("8934070100000000001", "ACTIVE")

    assert captured.get("operation") == "modifySubscription"
    assert "8934070100000000001" in captured.get("body", "")
    assert "lifeCycleStatus" in captured.get("body", "")
    assert "requestedStatus" not in captured.get("body", "")


@pytest.mark.asyncio
async def test_kite_adapter_set_administrative_status_respects_flag(monkeypatch):
    # When flag disabled, adapter raises UnsupportedOperation
    monkeypatch.setattr(
        kite_adapter_mod,
        "get_settings",
        lambda: SimpleNamespace(lifecycle_writes_enabled=False),
    )
    adapter = KiteAdapter()
    with pytest.raises(UnsupportedOperation):
        await adapter.set_administrative_status(
            "8934070100000000001",
            {},
            target=AdministrativeStatus.ACTIVE,
            idempotency_key="k",
        )


@pytest.mark.asyncio
async def test_kite_adapter_set_administrative_status_calls_client_when_enabled(
    monkeypatch,
):
    # When flag enabled, adapter should call KiteClient.modify_subscription with mapped native status
    monkeypatch.setattr(
        kite_adapter_mod,
        "get_settings",
        lambda: SimpleNamespace(lifecycle_writes_enabled=True),
    )

    called = {}

    async def fake_modify(self, iccid, requested_status):
        called["iccid"] = iccid
        called["requested_status"] = requested_status
        return ET.fromstring("<ok/>")

    monkeypatch.setattr(KiteClient, "modify_subscription", fake_modify)

    adapter = KiteAdapter()
    creds = {"endpoint": "https://kite.test/soap", "username": "u", "password": "p"}
    await adapter.set_administrative_status(
        "8934070100000000001",
        creds,
        target=AdministrativeStatus.ACTIVE,
        idempotency_key="k",
    )

    assert called.get("iccid") == "8934070100000000001"
    assert called.get("requested_status") == "ACTIVE"
