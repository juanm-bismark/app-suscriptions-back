"""Kite SOAP client.

This module isolates the transport and SOAP envelope construction from the
provider mapping logic so the adapter stays small and testable.
"""

from __future__ import annotations

import base64
import os
import ssl
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import escape
from typing import Any

import httpx
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
)
from cryptography.hazmat.primitives.serialization.pkcs12 import (
    load_key_and_certificates,
)

from app.shared.errors import (
    ProviderAuthFailed,
    ProviderForbidden,
    ProviderProtocolError,
    ProviderRateLimited,
    ProviderResourceNotFound,
    ProviderUnavailable,
    ProviderValidationError,
    UnsupportedOperation,
)

_SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
_KITE_TYPES_NS = (
    "http://www.telefonica.com/schemas/UNICA/SOAP/Globalm2m/inventory/v12/types"
)
_WSSE_NS = (
    "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
)


@dataclass(frozen=True)
class KiteCredentials:
    endpoint: str
    username: str | None = None
    password: str | None = None
    client_cert_pfx_b64: str | None = None
    client_cert_password: str | None = None


def _creds(data: dict[str, Any]) -> KiteCredentials:
    username = data.get("username")
    password = data.get("password")
    if bool(username) != bool(password):
        raise ProviderAuthFailed(
            detail="Kite WS-Security credentials require both username and password"
        )
    return KiteCredentials(
        endpoint=data["endpoint"],
        username=username,
        password=password,
        client_cert_pfx_b64=data.get("client_cert_pfx_b64") or data.get("pfx_base64"),
        client_cert_password=data.get("client_cert_password")
        or data.get("pfx_password"),
    )


def _qualified(tag: str) -> str:
    return f"<gm2minve_s3t:{tag}>"


def _envelope(creds: KiteCredentials, body_xml: str) -> bytes:
    header_xml = ""
    if creds.username and creds.password:
        header_xml = (
            "<soapenv:Header>"
            "<wsse:Security>"
            "<wsse:UsernameToken>"
            f"<wsse:Username>{escape(creds.username)}</wsse:Username>"
            f"<wsse:Password>{escape(creds.password)}</wsse:Password>"
            "</wsse:UsernameToken>"
            "</wsse:Security>"
            "</soapenv:Header>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soapenv:Envelope xmlns:soapenv="{_SOAP_ENV}" '
        f'xmlns:wsse="{_WSSE_NS}" '
        f'xmlns:gm2minve_s3t="{_KITE_TYPES_NS}">'
        f"{header_xml}"
        f"<soapenv:Body>{body_xml}</soapenv:Body>"
        "</soapenv:Envelope>"
    )
    return xml.encode("utf-8")


def _ssl_context(creds: KiteCredentials) -> ssl.SSLContext | bool:
    """Build a TLS context with an optional encrypted PFX client certificate.

    CompanyProviderCredentials.credentials_enc already stores provider secrets as
    Fernet-encrypted JSON. Kite certificate material should live there as
    `client_cert_pfx_b64` plus optional `client_cert_password`, never in
    account_scope or plaintext columns.
    """
    if not creds.client_cert_pfx_b64:
        return True

    try:
        pfx = base64.b64decode(creds.client_cert_pfx_b64)
        password = (
            creds.client_cert_password.encode("utf-8")
            if creds.client_cert_password
            else None
        )
        private_key, certificate, additional_certs = load_key_and_certificates(
            pfx, password
        )
    except Exception as exc:
        raise ProviderAuthFailed(
            detail=f"Kite client certificate could not be loaded: {exc}"
        ) from exc

    if private_key is None or certificate is None:
        raise ProviderAuthFailed(
            detail="Kite client certificate PFX is missing a private key or certificate"
        )

    context = ssl.create_default_context()
    cert_pem = certificate.public_bytes(Encoding.PEM)
    if additional_certs:
        cert_pem += b"".join(
            cert.public_bytes(Encoding.PEM) for cert in additional_certs
        )
    key_pem = private_key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        NoEncryption(),
    )

    cert_path = None
    key_path = None
    try:
        with tempfile.NamedTemporaryFile("wb", delete=False) as cert_file:
            cert_file.write(cert_pem)
            cert_path = cert_file.name
        with tempfile.NamedTemporaryFile("wb", delete=False) as key_file:
            key_file.write(key_pem)
            key_path = key_file.name
        context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    finally:
        for path in (cert_path, key_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    return context


async def _call(creds: KiteCredentials, operation: str, body_xml: str) -> ET.Element:
    async with httpx.AsyncClient(timeout=30.0, verify=_ssl_context(creds)) as client:
        try:
            response = await client.post(
                creds.endpoint,
                content=_envelope(creds, body_xml),
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": f'"urn:{operation}"',
                },
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(detail=f"Kite timeout on {operation}") from exc
        except httpx.RequestError as exc:
            raise ProviderUnavailable(detail=f"Kite network error: {exc}") from exc

    # Try to parse XML body first; if there's a SOAP Fault we should map it
    root = None
    try:
        root = ET.fromstring(response.text)
    except ET.ParseError:
        root = None

    if root is not None:
        fault = root.find(f".//{{{_SOAP_ENV}}}Fault")
        if fault is not None:
            # Try to extract structured fault detail following Kite's Fault/detail/ClientException|ServerException
            msg = fault.findtext("faultstring") or "unknown SOAP fault"
            detail_el = fault.find(".//detail")
            if detail_el is not None and len(list(detail_el)) > 0:
                # take the first child (ClientException or ServerException)
                exc_el = list(detail_el)[0]
                exception_id = exc_el.findtext("exceptionId") or ""
                exception_text = (
                    exc_el.findtext("text") or exc_el.findtext("message") or msg
                )
                soa_tx = exc_el.findtext("SOATransactionID") or exc_el.findtext(
                    "SOAConsumerTransactionID"
                )

                # Map known exception IDs/categories to domain exceptions
                # SVC.* → service errors (resource/validation)
                if exception_id == "SVC.1006":
                    raise ProviderResourceNotFound(
                        detail=exception_text,
                        provider_request_id=soa_tx,
                        provider_error_code=exception_id,
                        provider_error_message=exception_text,
                    )
                if exception_id == "SVC.1021":
                    raise ProviderValidationError(
                        detail=exception_text,
                        provider_request_id=soa_tx,
                        provider_error_code=exception_id,
                        provider_error_message=exception_text,
                    )
                # SVC.0002, SVC.0003, SVC.1000-1013, SVC.1020 -> validation
                if exception_id.startswith("SVC."):
                    try:
                        num = int(exception_id.split(".", 1)[1])
                    except Exception:
                        num = -1
                    if exception_id in {"SVC.0002", "SVC.0003", "SVC.1020"} or (
                        1000 <= num <= 1013
                    ):
                        raise ProviderValidationError(
                            detail=exception_text,
                            provider_request_id=soa_tx,
                            provider_error_code=exception_id,
                            provider_error_message=exception_text,
                        )

                # POL.1000 -> forbidden
                if exception_id == "POL.1000":
                    raise ProviderForbidden(
                        detail=exception_text,
                        provider_request_id=soa_tx,
                        provider_error_code=exception_id,
                        provider_error_message=exception_text,
                    )

                # SVR.* -> server-side issues
                if exception_id.startswith("SVR."):
                    if exception_id == "SVR.1003":
                        raise UnsupportedOperation(detail=exception_text)
                    if exception_id == "SVR.1006":
                        raise ProviderUnavailable(
                            detail=exception_text, extra={"retryable": True}
                        )
                    # fallback server-side error
                    raise ProviderUnavailable(
                        detail=exception_text,
                        extra={
                            "provider_request_id": soa_tx,
                            "provider_error_code": exception_id,
                            "provider_error_message": exception_text,
                        },
                    )

            # Fallback: unknown fault, return generic protocol error
            raise ProviderProtocolError(detail=f"Kite SOAP fault: {msg}")

    # No SOAP fault handled; fall back to HTTP status handling
    if response.status_code == 401:
        raise ProviderAuthFailed(detail="Kite authentication failed")
    if response.status_code == 429:
        raise ProviderRateLimited(detail="Kite rate limit exceeded")
    if response.status_code >= 500:
        raise ProviderUnavailable(detail=f"Kite HTTP {response.status_code}")
    if response.status_code >= 400:
        raise ProviderProtocolError(
            detail=f"Kite HTTP {response.status_code}: {response.text[:200]}"
        )

    return root


class KiteClient:
    """SOAP client for the Kite inventory API."""

    def __init__(self, credentials: dict[str, Any]):
        self._credentials = _creds(credentials)

    async def get_subscription_detail(self, iccid: str) -> ET.Element:
        body = (
            f"{_qualified('getSubscriptionDetail')}"
            f"{_qualified('icc')}{escape(iccid)}</gm2minve_s3t:icc>"
            f"</gm2minve_s3t:getSubscriptionDetail>"
        )
        return await _call(self._credentials, "getSubscriptionDetail", body)

    async def get_presence_detail(self, iccid: str) -> ET.Element:
        body = (
            f"{_qualified('getPresenceDetail')}"
            f"{_qualified('icc')}{escape(iccid)}</gm2minve_s3t:icc>"
            f"</gm2minve_s3t:getPresenceDetail>"
        )
        return await _call(self._credentials, "getPresenceDetail", body)

    async def get_subscriptions(
        self,
        *,
        start_index: int | None = None,
        batch_size: int | None = None,
        searchParameters: dict[str, str] | None = None,
        maxBatchSize: int | None = None,
    ) -> ET.Element:
        """Get subscriptions.

        Two calling styles supported for backward compatibility:
        - pagination style: provide `start_index` and `batch_size` (legacy)
        - search style: provide `searchParameters` and `maxBatchSize` (new)
        """
        # Build body in the WSDL sequence: maxBatchSize, startIndex, searchParameters.
        parts: list[str] = [f"{_qualified('getSubscriptions')}"]
        effective_batch_size = maxBatchSize if maxBatchSize is not None else batch_size
        if effective_batch_size is not None:
            parts.append(
                f"<gm2minve_s3t:maxBatchSize>{int(effective_batch_size)}</gm2minve_s3t:maxBatchSize>"
            )
        if start_index is not None:
            parts.append(
                f"<gm2minve_s3t:startIndex>{int(start_index)}</gm2minve_s3t:startIndex>"
            )
        if searchParameters is not None:
            parts.append("<gm2minve_s3t:searchParameters>")
            for name, value in searchParameters.items():
                parts.append("<gm2minve_s3t:param>")
                parts.append(f"<gm2minve_s3t:name>{escape(name)}</gm2minve_s3t:name>")
                parts.append(
                    f"<gm2minve_s3t:value>{escape(value)}</gm2minve_s3t:value>"
                )
                parts.append("</gm2minve_s3t:param>")
            parts.append("</gm2minve_s3t:searchParameters>")

        parts.append("</gm2minve_s3t:getSubscriptions>")
        body = "".join(parts)
        return await _call(self._credentials, "getSubscriptions", body)

    async def network_reset(self, iccid: str) -> ET.Element:
        body = (
            f"{_qualified('networkReset')}"
            f"<gm2minve_s3t:icc>{escape(iccid)}</gm2minve_s3t:icc>"
            f"<gm2minve_s3t:network2g3g>true</gm2minve_s3t:network2g3g>"
            f"<gm2minve_s3t:network4g>true</gm2minve_s3t:network4g>"
            f"</gm2minve_s3t:networkReset>"
        )
        return await _call(self._credentials, "networkReset", body)

    async def modify_subscription(
        self, iccid: str, requested_status: str
    ) -> ET.Element:
        """Modify subscription lifecycle/status.

        `requested_status` is our normalized/internal argument name. The Kite
        SOAP contract calls the provider-side XML field `lifeCycleStatus`.
        """
        body = (
            f"{_qualified('modifySubscription')}"
            f"<gm2minve_s3t:icc>{escape(iccid)}</gm2minve_s3t:icc>"
            f"<gm2minve_s3t:lifeCycleStatus>{escape(requested_status)}</gm2minve_s3t:lifeCycleStatus>"
            f"</gm2minve_s3t:modifySubscription>"
        )
        return await _call(self._credentials, "modifySubscription", body)

    async def get_status_detail(self, iccid: str) -> ET.Element:
        """Get current status details for a subscription (state, change reason, timestamp)."""
        body = (
            f"{_qualified('getStatusDetail')}"
            f"{_qualified('icc')}{escape(iccid)}</gm2minve_s3t:icc>"
            f"</gm2minve_s3t:getStatusDetail>"
        )
        return await _call(self._credentials, "getStatusDetail", body)

    async def get_status_history(
        self, iccid: str, start_date: str | None = None, end_date: str | None = None
    ) -> ET.Element:
        """Get status history for a subscription over a date range (inclusive of start/end)."""
        body_parts = [
            f"{_qualified('getStatusHistory')}",
            f"{_qualified('icc')}{escape(iccid)}</gm2minve_s3t:icc>",
        ]
        if start_date:
            body_parts.append(
                f"<gm2minve_s3t:startDate>{escape(start_date)}</gm2minve_s3t:startDate>"
            )
        if end_date:
            body_parts.append(
                f"<gm2minve_s3t:endDate>{escape(end_date)}</gm2minve_s3t:endDate>"
            )
        body_parts.append("</gm2minve_s3t:getStatusHistory>")
        body = "".join(body_parts)
        return await _call(self._credentials, "getStatusHistory", body)
