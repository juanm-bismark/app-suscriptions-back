"""Tests for Kite SOAP fault parsing and exception mapping."""

import pytest

import httpx

from app.providers.kite.client import KiteClient
from app.shared.errors import (
    ProviderResourceNotFound,
    ProviderValidationError,
    ProviderForbidden,
    ProviderUnavailable,
)


async def _fake_post_factory(body: str):
    async def _fake_post(_client: httpx.AsyncClient, _url: str, content=None, headers=None):
        return httpx.Response(500, text=body)

    return _fake_post


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

    monkeypatch.setattr(httpx.AsyncClient, "post", await _fake_post_factory(body))
    client = KiteClient({"endpoint": "https://kite.test", "username": "u", "password": "p"})
    with pytest.raises(ProviderUnavailable) as excinfo:
        await client.get_subscription_detail("8934070100000000001")
    exc = excinfo.value
    # Ensure retryable flag surfaced in extra
    assert exc.extra.get("retryable") is True
