"""Typed credential DTOs for Kite provider adapter."""

from typing import TypedDict


class KiteCredentials(TypedDict, total=False):
    endpoint: str
    # Optional WS-Security UsernameToken credentials. Cert-only Kite accounts
    # should omit both; if one is set, the other must be set too.
    username: str
    password: str
    # Encrypted inside company_provider_credentials.credentials_enc.
    # Contains base64-encoded PFX/PKCS#12 client certificate downloaded from Kite.
    client_cert_pfx_b64: str
    client_cert_password: str
    company_custom_field: str
    company_id: str
