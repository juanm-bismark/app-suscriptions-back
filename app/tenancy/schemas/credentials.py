from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, Field

from app.tenancy.credential_expiry import CredentialExpiryStatus

PROVIDER_CREDENTIAL_EXAMPLES = {
    "kite": {
        "summary": "Kite SOAP certificate credentials",
        "description": (
            "Use this for Kite accounts authenticated with a PFX/PKCS#12 "
            "client certificate. Include username/password only when Kite "
            "also issued WS-Security SOAP credentials."
        ),
        "value": {
            "credentials": {
                "endpoint": "https://kiteplatform-api.telefonica.com:8010/services/SOAP/GlobalM2M/Inventory/v12/r12",
                "username": "KITE_USERNAME_OPTIONAL",
                "password": "KITE_PASSWORD_OPTIONAL",
                "client_cert_pfx_b64": "BASE64_OF_THE_PFX_FILE",
                "client_cert_password": "PFX_PASSWORD",
                "server_ca_bundle_pem_b64": "BASE64_OF_TELEFONICA_CA_PEM_OPTIONAL",
            },
            "account_scope": {
                "environment": "production",
                "end_customer_id": "KITE_END_CUSTOMER_ID",
                "cert_expires_at": "2026-12-31T00:00:00Z",
            },
        },
    },
    "tele2": {
        "summary": "Tele2 Cisco Control Center credentials",
        "description": (
            "Tele2 uses Cisco Control Center REST Basic auth. If base_url or "
            "cobrand_url is omitted, the API defaults to restapi3.jasper.com. "
            "api_version defaults to v1; only v1/1 is supported."
        ),
        "value": {
            "credentials": {
                "cobrand_url": "restapi3.jasper.com",
                "username": "TELE2_USERNAME",
                "api_key": "TELE2_API_KEY",
                "api_version": "v1",
            },
            "account_scope": {
                "account_id": "TELE2_ACCOUNT_ID",
                "max_tps": 5,
                "environment": "production",
            },
        },
    },
    "moabits": {
        "summary": "Moabits Orion credentials",
        "description": (
            "Moabits defaults to Orion v1 REST JWT auth. Provide x_api_key, the Orion "
            "Web Client application key. The adapter sends it as the x-api-key "
            "header to GET /integrity/authorization-token, caches the returned "
            "JWT, and refreshes/retries once when a provider business endpoint "
            "returns 401. parent_company_code is used to discover child companies "
            "through GET /api/company/childs/{companyCode}. Configure selected "
            "Moabits company codes through the admin-only PUT /moabits/company-codes; "
            "the selection is stored as non-secret provider source config."
        ),
        "value": {
            "credentials": {
                "base_url": "https://www.api.myorion.co",
                "x_api_key": "MOABITS_ORION_X_API_KEY",
                "parent_company_code": "MOABITS_PARENT_COMPANY_CODE",
            },
            "account_scope": {
                "parent_company_code": "MOABITS_PARENT_COMPANY_CODE",
                "environment": "production",
            },
        },
    },
}


class CredentialMetadataOut(BaseModel):
    provider: str
    active: bool
    rotated_at: datetime | None
    created_at: datetime
    account_scope: dict[str, Any] = Field(default_factory=dict)
    expiry_status: CredentialExpiryStatus


class CredentialUpsertIn(BaseModel):
    credentials: dict[str, Any] = Field(
        description=(
            "Secret provider credentials. Shape depends on the path provider "
            "(`kite`, `tele2`, or `moabits`) and is encrypted before storage."
        ),
    )
    account_scope: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Non-secret account metadata such as environment, account IDs, "
            "company codes, rate limits, or expiry timestamps."
        ),
    )


class CredentialTestOut(BaseModel):
    provider: str
    ok: bool
    detail: str | None = None


class MoabitsCompanyOut(BaseModel):
    company_code: str = Field(serialization_alias="companyCode")
    company_name: str = Field(serialization_alias="companyName")
    clie_id: int | None = None


class MoabitsCompanyDiscoveryOut(BaseModel):
    current_company_name: str
    selected_company_codes: list[str] = Field(
        default_factory=list,
        description="Moabits company codes already configured for this source.",
    )
    selected_companies: list[MoabitsCompanyOut] = Field(
        default_factory=list,
        description=(
            "Discovered Moabits companies whose companyCode is already configured "
            "for this source."
        ),
    )
    companies: list[MoabitsCompanyOut]


class MoabitsCompanySelectionItemIn(BaseModel):
    company_code: str = Field(
        validation_alias=AliasChoices("companyCode", "company_code"),
        serialization_alias="companyCode",
        description="Moabits company code selected for this source.",
    )
    company_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("companyName", "company_name"),
        serialization_alias="companyName",
        description="Optional Moabits company name from the discovery response.",
    )
    clie_id: int | None = None


class MoabitsCompanySelectionIn(BaseModel):
    company_codes: list[MoabitsCompanySelectionItemIn] = Field(
        min_length=1,
        description=(
            "Moabits companies selected for this source, using the objects "
            "returned by discovery."
        ),
    )
