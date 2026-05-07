import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.database import get_db
from app.identity.dependencies import get_current_profile
from app.identity.models.profile import AppRole, Profile
from app.shared.crypto import decrypt_credentials, encrypt_credentials
from app.shared.errors import DomainError, ProviderAuthFailed
from app.tenancy.credential_expiry import credential_expiry_status
from app.tenancy.models.company import Company
from app.tenancy.models.credentials import CompanyProviderCredentials
from app.tenancy.routers import credentials as credentials_router

COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
FERNET_KEY = Fernet.generate_key().decode()


class _Scalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return self._rows


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Scalars:
        return _Scalars(self._rows)

    def scalar_one_or_none(self) -> Any | None:
        if not self._rows:
            return None
        return self._rows[0]


class _Db:
    def __init__(
        self,
        rows: list[CompanyProviderCredentials] | None = None,
        *,
        company_name: str = "Bismark Colombia",
    ) -> None:
        self.rows = rows or []
        self.commits = 0
        self.company = Company(id=COMPANY_ID, name=company_name)

    async def execute(self, statement: Any) -> _Result:
        if getattr(statement, "__visit_name__", "") == "update":
            for row in self.rows:
                if row.company_id == COMPANY_ID and row.active:
                    row.active = False
            return _Result([])
        if "FROM companies" in str(statement):
            return _Result([self.company])
        return _Result(
            [row for row in self.rows if row.company_id == COMPANY_ID and row.active]
        )

    def add(self, row: CompanyProviderCredentials) -> None:
        self.rows.append(row)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, row: CompanyProviderCredentials) -> None:
        return None


class _Provider:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []
        self.cursors: list[str | None] = []

    async def list_subscriptions(
        self,
        credentials: dict[str, Any],
        *,
        cursor: str | None,
        limit: int,
        filters: Any = None,
    ) -> tuple[list[Any], None]:
        self.calls.append(credentials)
        self.cursors.append(cursor)
        if self.fail:
            raise ProviderAuthFailed(detail="bad provider credentials")
        return [], None


class _Registry:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider

    def get(self, provider: str) -> _Provider:
        return self.provider


def _row(
    provider: str = "tele2",
    account_scope: dict[str, Any] | None = None,
    credentials: dict[str, Any] | None = None,
) -> CompanyProviderCredentials:
    now = datetime.now(UTC)
    return CompanyProviderCredentials(
        id=uuid.uuid4(),
        company_id=COMPANY_ID,
        provider=provider,
        credentials_enc=encrypt_credentials(
            credentials or {"api_key": "old-secret"}, FERNET_KEY
        ),
        account_scope=account_scope or {},
        active=True,
        rotated_at=now,
        created_at=now,
    )


def _profile(role: AppRole) -> Profile:
    return Profile(id=USER_ID, company_id=COMPANY_ID, role=role)


def _client(
    role: AppRole,
    db: _Db | None = None,
    provider: _Provider | None = None,
) -> TestClient:
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": exc.code, "detail": exc.detail, **exc.extra},
        )

    async def _db_override():
        yield db or _Db()

    app.include_router(credentials_router.router, prefix="/v1")
    app.dependency_overrides[get_current_profile] = lambda: _profile(role)
    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_settings] = lambda: Settings(fernet_key=FERNET_KEY)
    app.dependency_overrides[credentials_router.get_registry] = lambda: _Registry(
        provider or _Provider()
    )
    return TestClient(app)


def test_manager_can_list_credential_metadata_without_secrets() -> None:
    db = _Db([_row(account_scope={"account_id": "acct-1"})])
    client = _client(AppRole.manager, db)

    response = client.get("/v1/companies/me/credentials")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["provider"] == "tele2"
    assert payload[0]["account_scope"] == {"account_id": "acct-1"}
    serialized = response.text
    assert "old-secret" not in serialized
    assert "credentials_enc" not in serialized
    assert "api_key" not in serialized


def test_member_cannot_manage_credentials() -> None:
    client = _client(AppRole.member, _Db([_row()]))

    response = client.get("/v1/companies/me/credentials")

    assert response.status_code == 403


def test_test_endpoint_returns_provider_failure_without_persisting() -> None:
    db = _Db([])
    provider = _Provider(fail=True)
    client = _client(AppRole.admin, db, provider)

    response = client.post(
        "/v1/companies/me/credentials/tele2/test",
        json={"credentials": {"username": "u", "api_key": "k"}},
    )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "tele2",
        "ok": False,
        "detail": "bad provider credentials",
    }
    assert db.rows == []


def test_patch_rotates_and_encrypts_credentials() -> None:
    old = _row()
    db = _Db([old])
    provider = _Provider()
    client = _client(AppRole.manager, db, provider)

    response = client.patch(
        "/v1/companies/me/credentials/tele2",
        json={
            "credentials": {
                "username": "alice",
                "api_key": "new-secret",
            },
            "account_scope": {"account_id": "acct-2", "max_tps": 5},
        },
    )

    assert response.status_code == 200
    assert old.active is False
    assert len(db.rows) == 2
    created = db.rows[-1]
    assert created.active is True
    assert created.rotated_at is not None
    assert created.account_scope == {"account_id": "acct-2", "max_tps": 5}
    decrypted = decrypt_credentials(created.credentials_enc, FERNET_KEY)
    assert decrypted["api_key"] == "new-secret"
    assert decrypted["base_url"] == "https://restapi3.jasper.com"
    assert "api_version" not in decrypted
    assert "cobrand_url" not in decrypted
    assert provider.calls[0]["company_id"] == str(COMPANY_ID)
    assert provider.calls[0]["base_url"] == "https://restapi3.jasper.com"
    assert provider.calls[0]["max_tps"] == 5
    assert provider.cursors[0] is not None
    assert "since:" in provider.cursors[0]
    assert "new-secret" not in response.text


def test_patch_allows_custom_tele2_cobrand_url() -> None:
    db = _Db([])
    provider = _Provider()
    client = _client(AppRole.manager, db, provider)

    response = client.patch(
        "/v1/companies/me/credentials/tele2",
        json={
            "credentials": {
                "cobrand_url": "custom.jasper.example/rws/api/v1/devices",
                "username": "alice",
                "api_key": "secret",
            }
        },
    )

    assert response.status_code == 200
    decrypted = decrypt_credentials(db.rows[-1].credentials_enc, FERNET_KEY)
    assert decrypted["base_url"] == "https://custom.jasper.example"
    assert provider.calls[0]["base_url"] == "https://custom.jasper.example"


def test_patch_kite_requires_certificate_credentials() -> None:
    db = _Db([])
    provider = _Provider()
    client = _client(AppRole.manager, db, provider)

    response = client.patch(
        "/v1/companies/me/credentials/kite",
        json={
            "credentials": {
                "endpoint": "https://kite.test/soap",
            },
            "account_scope": {"environment": "production"},
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Kite client_cert_pfx_b64 is required"
    assert db.rows == []
    assert provider.calls == []


def test_patch_kite_rotates_certificate_credentials_without_returning_secrets() -> None:
    db = _Db([])
    provider = _Provider()
    client = _client(AppRole.manager, db, provider)

    response = client.patch(
        "/v1/companies/me/credentials/kite",
        json={
            "credentials": {
                "endpoint": "https://kite.test/soap/",
                "client_cert_pfx_b64": "BASE64-PFX",
                "client_cert_password": "pfx-secret",
                "server_ca_cert_pem_b64": "BASE64-CA",
            },
            "account_scope": {
                "environment": "production",
                "end_customer_id": "end-customer-1",
                "cert_expires_at": "2026-12-31T00:00:00Z",
            },
        },
    )

    assert response.status_code == 200
    assert len(db.rows) == 1
    decrypted = decrypt_credentials(db.rows[-1].credentials_enc, FERNET_KEY)
    assert decrypted["endpoint"] == "https://kite.test/soap"
    assert decrypted["client_cert_pfx_b64"] == "BASE64-PFX"
    assert decrypted["client_cert_password"] == "pfx-secret"
    assert decrypted["server_ca_bundle_pem_b64"] == "BASE64-CA"
    assert "server_ca_cert_pem_b64" not in decrypted
    assert decrypted["end_customer_id"] == "end-customer-1"
    assert provider.calls[0]["company_id"] == str(COMPANY_ID)
    assert provider.calls[0]["client_cert_pfx_b64"] == "BASE64-PFX"
    assert provider.calls[0]["server_ca_bundle_pem_b64"] == "BASE64-CA"
    assert "BASE64-PFX" not in response.text
    assert "pfx-secret" not in response.text


def test_failed_patch_does_not_modify_database() -> None:
    old = _row()
    db = _Db([old])
    client = _client(AppRole.admin, db, _Provider(fail=True))

    response = client.patch(
        "/v1/companies/me/credentials/tele2",
        json={"credentials": {"username": "u", "api_key": "bad"}},
    )

    assert response.status_code == 422
    assert old.active is True
    assert len(db.rows) == 1
    assert db.commits == 0


def test_only_admin_can_deactivate_credentials() -> None:
    db = _Db([_row()])
    manager = _client(AppRole.manager, db)
    admin = _client(AppRole.admin, db)

    manager_response = manager.delete("/v1/companies/me/credentials/tele2")
    admin_response = admin.delete("/v1/companies/me/credentials/tele2")

    assert manager_response.status_code == 403
    assert admin_response.status_code == 204
    assert db.rows[0].active is False


def test_discover_moabits_companies_matches_local_company(monkeypatch) -> None:
    async def _children(credentials: dict[str, Any]) -> list[dict[str, Any]]:
        assert credentials["x_api_key"] == "secret-key"
        return [
            {
                "companyCode": "48123-99",
                "companyName": "3 POINTECH S.A.S.",
                "clie_id": 2659,
            },
            {
                "companyCode": "48123",
                "companyName": "Bismark Colombia",
                "clie_id": 132,
            },
        ]

    monkeypatch.setattr(credentials_router, "fetch_child_companies", _children)
    db = _Db(
        [
            _row(
                provider="moabits",
                account_scope={"company_codes": ["48123"]},
                credentials={
                    "base_url": "https://www.api.myorion.co",
                    "x_api_key": "secret-key",
                    "parent_company_code": "48123",
                    "company_codes": ["48123"],
                },
            )
        ],
        company_name="Bismark Colombia",
    )
    client = _client(AppRole.manager, db)

    response = client.get("/v1/companies/me/credentials/moabits/companies/discover")

    assert response.status_code == 200
    payload = response.json()
    assert payload["current_company_name"] == "Bismark Colombia"
    assert payload["companies"][0] == {
        "company_code": "48123",
        "company_name": "Bismark Colombia",
        "clie_id": 132,
        "selected": True,
        "matches_current_company": True,
    }


def test_select_moabits_company_codes_validates_and_persists(monkeypatch) -> None:
    async def _children(credentials: dict[str, Any]) -> list[dict[str, Any]]:
        assert credentials["x_api_key"] == "secret-key"
        return [
            {
                "companyCode": "48123",
                "companyName": "Bismark Colombia",
                "clie_id": 132,
            },
            {
                "companyCode": "48123-99",
                "companyName": "3 POINTECH S.A.S.",
                "clie_id": 2659,
            },
        ]

    monkeypatch.setattr(credentials_router, "fetch_child_companies", _children)
    row = _row(
        provider="moabits",
        credentials={
            "base_url": "https://www.api.myorion.co",
            "x_api_key": "secret-key",
            "parent_company_code": "48123",
            "company_codes": [],
        },
    )
    db = _Db([row])
    client = _client(AppRole.admin, db)

    response = client.put(
        "/v1/companies/me/credentials/moabits/company-codes",
        json={"company_codes": ["48123", "48123-99"]},
    )

    assert response.status_code == 200
    assert db.commits == 1
    assert row.account_scope["company_codes"] == ["48123", "48123-99"]
    decrypted = decrypt_credentials(row.credentials_enc, FERNET_KEY)
    assert decrypted["company_codes"] == ["48123", "48123-99"]
    assert "secret-key" not in response.text


def test_select_moabits_company_codes_rejects_unavailable_codes(monkeypatch) -> None:
    async def _children(credentials: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"companyCode": "48123", "companyName": "Bismark Colombia"}]

    monkeypatch.setattr(credentials_router, "fetch_child_companies", _children)
    db = _Db(
        [
            _row(
                provider="moabits",
                credentials={
                    "base_url": "https://www.api.myorion.co",
                    "x_api_key": "secret-key",
                    "parent_company_code": "48123",
                },
            )
        ]
    )
    client = _client(AppRole.manager, db)

    response = client.put(
        "/v1/companies/me/credentials/moabits/company-codes",
        json={"company_codes": ["48123-404"]},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["company_codes"] == ["48123-404"]
    assert db.commits == 0


def test_expiry_statuses() -> None:
    now = datetime(2026, 5, 6, tzinfo=UTC)

    assert credential_expiry_status({}, now=now).value == "valid"
    assert (
        credential_expiry_status(
            {"cert_expires_at": (now + timedelta(days=40)).isoformat()},
            now=now,
        ).value
        == "valid"
    )
    assert (
        credential_expiry_status(
            {"cert_expires_at": (now + timedelta(days=10)).isoformat()},
            now=now,
        ).value
        == "expiring"
    )
    assert (
        credential_expiry_status(
            {"token_expires_at": (now - timedelta(days=1)).isoformat()},
            now=now,
        ).value
        == "expired"
    )
    assert (
        credential_expiry_status({"cert_expires_at": "not-a-date"}, now=now).value
        == "invalid"
    )


def test_credential_endpoints_document_provider_specific_examples() -> None:
    client = _client(AppRole.admin)

    response = client.get("/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    for method_path, method in [
        ("/v1/companies/me/credentials/{provider}/test", "post"),
        ("/v1/companies/me/credentials/{provider}", "patch"),
    ]:
        examples = paths[method_path][method]["requestBody"]["content"][
            "application/json"
        ]["examples"]
        assert set(examples) == {"kite", "tele2", "moabits"}
        assert examples["kite"]["value"]["credentials"]["client_cert_pfx_b64"]
        assert examples["tele2"]["value"]["credentials"]["api_key"]
        assert examples["tele2"]["value"]["credentials"]["api_version"] == "v1"
        assert examples["moabits"]["value"]["credentials"]["x_api_key"]
        assert examples["moabits"]["value"]["credentials"]["company_codes"]
