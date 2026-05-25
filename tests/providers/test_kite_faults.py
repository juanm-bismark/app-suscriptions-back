"""Tests for Kite SOAP fault parsing and exception mapping."""

import pytest

import httpx

from app.providers.kite import client as kite_client
from app.providers.kite.client import KiteClient
from app.shared.errors import (
    ProviderForbidden,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderResourceNotFound,
    ProviderUnavailable,
    ProviderValidationError,
)


async def _fake_post_factory(body: str):
    async def _fake_post(_client: httpx.AsyncClient, _url: str, content=None, headers=None):
        return httpx.Response(500, text=body)

    return _fake_post


def _disable_retries(monkeypatch) -> None:
    monkeypatch.setattr(kite_client, "_retry_max_attempts", lambda: 1)


@pytest.mark.asyncio
async def test_svc_1006_maps_to_not_found(monkeypatch):
    body = """
    <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
      <soapenv:Body>
        <soapenv:Fault>
          <faultcode>soap:Client</faultcode>
          <faultstring>Not found</faultstring>
          <detail>
            <ClientException>
              <exceptionCategory>SVC</exceptionCategory>
              <exceptionId>SVC.1006</exceptionId>
              <text>Subscription not found</text>
              <SOATransactionID>tx-123</SOATransactionID>
            </ClientException>
          </detail>
        </soapenv:Fault>
      </soapenv:Body>
    </soapenv:Envelope>
    """

    monkeypatch.setattr(httpx.AsyncClient, "post", await _fake_post_factory(body))
    client = KiteClient({"endpoint": "https://kite.test", "username": "u", "password": "p"})
    with pytest.raises(ProviderResourceNotFound) as excinfo:
        await client.get_subscription_detail("8934070100000000001")
    exc = excinfo.value
    assert getattr(exc, "provider_request_id", None) == "tx-123"
    assert getattr(exc, "provider_error_code", None) == "SVC.1006"
    assert exc.extra["provider_error_code"] == "SVC.1006"


@pytest.mark.asyncio
async def test_svc_validation_maps_to_422(monkeypatch):
    body = """
    <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
      <soapenv:Body>
        <soapenv:Fault>
          <faultcode>soap:Client</faultcode>
          <faultstring>Validation error</faultstring>
          <detail>
            <ClientException>
              <exceptionCategory>SVC</exceptionCategory>
              <exceptionId>SVC.1021</exceptionId>
              <text>Invalid transition</text>
              <SOATransactionID>tx-422</SOATransactionID>
            </ClientException>
          </detail>
        </soapenv:Fault>
      </soapenv:Body>
    </soapenv:Envelope>
    """

    monkeypatch.setattr(httpx.AsyncClient, "post", await _fake_post_factory(body))
    client = KiteClient({"endpoint": "https://kite.test", "username": "u", "password": "p"})
    with pytest.raises(ProviderValidationError) as excinfo:
        await client.get_subscription_detail("8934070100000000001")
    exc = excinfo.value
    assert getattr(exc, "provider_request_id", None) == "tx-422"
    assert getattr(exc, "provider_error_code", None) == "SVC.1021"


@pytest.mark.asyncio
async def test_pol_forbidden_maps_to_403(monkeypatch):
    body = """
    <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
      <soapenv:Body>
        <soapenv:Fault>
          <faultcode>soap:Client</faultcode>
          <faultstring>Forbidden</faultstring>
          <detail>
            <ClientException>
              <exceptionCategory>POL</exceptionCategory>
              <exceptionId>POL.1000</exceptionId>
              <text>Operation forbidden</text>
              <SOATransactionID>tx-403</SOATransactionID>
            </ClientException>
          </detail>
        </soapenv:Fault>
      </soapenv:Body>
    </soapenv:Envelope>
    """

    monkeypatch.setattr(httpx.AsyncClient, "post", await _fake_post_factory(body))
    client = KiteClient({"endpoint": "https://kite.test", "username": "u", "password": "p"})
    with pytest.raises(ProviderForbidden) as excinfo:
        await client.get_subscription_detail("8934070100000000001")
    exc = excinfo.value
    assert getattr(exc, "provider_request_id", None) == "tx-403"
    assert getattr(exc, "provider_error_code", None) == "POL.1000"


@pytest.mark.asyncio
async def test_rate_period_fault_maps_to_rate_limited(monkeypatch):
    body = """
    <soapenv:Envelope
        xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
        xmlns:com="http://www.telefonica.com/schemas/UNICA/SOAP/common/v1">
      <soapenv:Body>
        <soapenv:Fault>
          <faultcode>soap:Client</faultcode>
          <faultstring>Client exception</faultstring>
          <detail>
            <com:ClientException>
              <com:exceptionCategory>POL</com:exceptionCategory>
              <com:exceptionId>0001</com:exceptionId>
              <com:text>Request rate per period control not pass.</com:text>
              <com:SOATransactionID>tx-rate</com:SOATransactionID>
            </com:ClientException>
          </detail>
        </soapenv:Fault>
      </soapenv:Body>
    </soapenv:Envelope>
    """

    _disable_retries(monkeypatch)
    monkeypatch.setattr(httpx.AsyncClient, "post", await _fake_post_factory(body))
    client = KiteClient({"endpoint": "https://kite.test", "username": "u", "password": "p"})
    with pytest.raises(ProviderRateLimited) as excinfo:
        await client.get_subscription_detail("8934070100000000001")
    exc = excinfo.value
    assert exc.detail == "Request rate per period control not pass."
    assert exc.extra["provider_request_id"] == "tx-rate"
    assert exc.extra["provider_error_code"] == "POL.0001"


@pytest.mark.asyncio
async def test_retryable_rate_fault_retries_read_operation(monkeypatch):
    fault_body = """
    <soapenv:Envelope
        xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
        xmlns:com="http://www.telefonica.com/schemas/UNICA/SOAP/common/v1">
      <soapenv:Body>
        <soapenv:Fault>
          <faultcode>soap:Client</faultcode>
          <faultstring>Client exception</faultstring>
          <detail>
            <com:ClientException>
              <com:exceptionCategory>POL</com:exceptionCategory>
              <com:exceptionId>0001</com:exceptionId>
              <com:text>Request rate per period control not pass.</com:text>
            </com:ClientException>
          </detail>
        </soapenv:Fault>
      </soapenv:Body>
    </soapenv:Envelope>
    """
    success_body = """
    <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
      <soapenv:Body><ok /></soapenv:Body>
    </soapenv:Envelope>
    """
    calls = 0

    async def _fake_post(_client: httpx.AsyncClient, _url: str, content=None, headers=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, text=fault_body)
        return httpx.Response(200, text=success_body)

    monkeypatch.setattr(kite_client, "_retry_max_attempts", lambda: 2)
    monkeypatch.setattr(kite_client, "_retry_delay_seconds", lambda _attempt: 0)
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    client = KiteClient({"endpoint": "https://kite.test", "username": "u", "password": "p"})
    root = await client.get_subscription_detail("8934070100000000001")

    assert root.find(".//ok") is not None
    assert calls == 2


@pytest.mark.asyncio
async def test_network_reset_does_not_retry_rate_fault(monkeypatch):
    body = """
    <soapenv:Envelope
        xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
        xmlns:com="http://www.telefonica.com/schemas/UNICA/SOAP/common/v1">
      <soapenv:Body>
        <soapenv:Fault>
          <faultcode>soap:Client</faultcode>
          <faultstring>Client exception</faultstring>
          <detail>
            <com:ClientException>
              <com:exceptionCategory>POL</com:exceptionCategory>
              <com:exceptionId>0001</com:exceptionId>
              <com:text>Request rate per period control not pass.</com:text>
            </com:ClientException>
          </detail>
        </soapenv:Fault>
      </soapenv:Body>
    </soapenv:Envelope>
    """
    calls = 0

    async def _fake_post(_client: httpx.AsyncClient, _url: str, content=None, headers=None):
        nonlocal calls
        calls += 1
        return httpx.Response(500, text=body)

    monkeypatch.setattr(kite_client, "_retry_max_attempts", lambda: 2)
    monkeypatch.setattr(kite_client, "_retry_delay_seconds", lambda _attempt: 0)
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    client = KiteClient({"endpoint": "https://kite.test", "username": "u", "password": "p"})
    with pytest.raises(ProviderRateLimited):
        await client.network_reset("8934070100000000001")

    assert calls == 1


@pytest.mark.asyncio
async def test_svr_1006_retryable(monkeypatch):
    body = """
    <soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">
      <soapenv:Body>
        <soapenv:Fault>
          <faultcode>soap:Server</faultcode>
          <faultstring>Transient server error</faultstring>
          <detail>
            <ServerException>
              <exceptionCategory>SVR</exceptionCategory>
              <exceptionId>SVR.1006</exceptionId>
              <text>Temporary outage</text>
              <SOATransactionID>tx-500</SOATransactionID>
            </ServerException>
          </detail>
        </soapenv:Fault>
      </soapenv:Body>
    </soapenv:Envelope>
    """

    _disable_retries(monkeypatch)
    monkeypatch.setattr(httpx.AsyncClient, "post", await _fake_post_factory(body))
    client = KiteClient({"endpoint": "https://kite.test", "username": "u", "password": "p"})
    with pytest.raises(ProviderUnavailable) as excinfo:
        await client.get_subscription_detail("8934070100000000001")
    exc = excinfo.value
    # Ensure retryable flag surfaced in extra
    assert exc.extra.get("retryable") is True


@pytest.mark.asyncio
async def test_namespaced_client_exception_detail_is_extracted(monkeypatch):
    body = """
    <soapenv:Envelope
        xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
        xmlns:com="http://www.telefonica.com/schemas/UNICA/SOAP/common/v1">
      <soapenv:Body>
        <soapenv:Fault>
          <faultcode>soap:Client</faultcode>
          <faultstring>Client exception</faultstring>
          <detail>
            <com:ClientException>
              <com:exceptionCategory>SVC</com:exceptionCategory>
              <com:exceptionId>1021</com:exceptionId>
              <com:text>Invalid search parameter icc</com:text>
              <com:SOATransactionID>tx-ns-422</com:SOATransactionID>
            </com:ClientException>
          </detail>
        </soapenv:Fault>
      </soapenv:Body>
    </soapenv:Envelope>
    """

    monkeypatch.setattr(httpx.AsyncClient, "post", await _fake_post_factory(body))
    client = KiteClient({"endpoint": "https://kite.test", "username": "u", "password": "p"})
    with pytest.raises(ProviderValidationError) as excinfo:
        await client.get_subscription_detail("8934070100000000001")
    exc = excinfo.value
    assert exc.detail == "Invalid search parameter icc"
    assert exc.extra["provider_request_id"] == "tx-ns-422"
    assert exc.extra["provider_error_code"] == "SVC.1021"


@pytest.mark.asyncio
async def test_unknown_namespaced_client_exception_keeps_provider_detail(monkeypatch):
    body = """
    <soapenv:Envelope
        xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
        xmlns:com="http://www.telefonica.com/schemas/UNICA/SOAP/common/v1">
      <soapenv:Body>
        <soapenv:Fault>
          <faultcode>soap:Client</faultcode>
          <faultstring>Client exception</faultstring>
          <detail>
            <com:ClientException>
              <com:exceptionCategory>CLI</com:exceptionCategory>
              <com:exceptionId>9999</com:exceptionId>
              <com:text>Malformed SOAP body</com:text>
              <com:SOATransactionID>tx-unknown</com:SOATransactionID>
            </com:ClientException>
          </detail>
        </soapenv:Fault>
      </soapenv:Body>
    </soapenv:Envelope>
    """

    monkeypatch.setattr(httpx.AsyncClient, "post", await _fake_post_factory(body))
    client = KiteClient({"endpoint": "https://kite.test", "username": "u", "password": "p"})
    with pytest.raises(ProviderProtocolError) as excinfo:
        await client.get_subscription_detail("8934070100000000001")
    exc = excinfo.value
    assert exc.detail == "Malformed SOAP body"
    assert exc.extra["provider_request_id"] == "tx-unknown"
    assert exc.extra["provider_error_code"] == "CLI.9999"
