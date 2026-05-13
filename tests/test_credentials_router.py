import base64
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
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
from app.tenancy.models.provider_mapping import CompanyProviderMapping
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
        moabits_mapping: CompanyProviderMapping | None = None,
    ) -> None:
        self.rows = rows or []
        self.moabits_mapping = moabits_mapping
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
        if "FROM company_provider_mappings" in str(statement):
            return _Result([self.moabits_mapping] if self.moabits_mapping else [])
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

    def supports_list_filter(self, filter_name: str) -> bool:
        return False

    def bootstrap_filters(self) -> Any:
        return None


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


def _moabits_mapping() -> CompanyProviderMapping:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return CompanyProviderMapping(
        company_id=COMPANY_ID,
        provider="moabits",
        provider_company_code="48123",
        provider_company_name="Bismark Colombia",
        settings={},
        active=True,
        created_at=now,
        updated_at=now,
    )


def _kite_pfx_b64(
    expires_at: datetime,
    *,
    password: str = "pfx-secret",
) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "kite-test-client"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(expires_at)
        .sign(key, hashes.SHA256())
    )
    pfx = pkcs12.serialize_key_and_certificates(
        name=b"kite-test-client",
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(
            password.encode("utf-8")
        ),
    )
    return base64.b64encode(pfx).decode("ascii")


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
    app.include_router(credentials_router.admin_credentials_router, prefix="/v1")
    app.include_router(
        credentials_router.admin_company_credentials_router,
        prefix="/v1",
    )
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


def test_credential_listing_filters_by_provider_or_account_scope() -> None:
    query = credentials_router._list_credentials_query(COMPANY_ID, "acct")

    query_text = str(query)
    assert "lower(company_provider_credentials.provider)" in query_text
    assert "company_provider_credentials.account_scope" in query_text


def test_credential_listing_ignores_blank_q() -> None:
    query = credentials_router._list_credentials_query(COMPANY_ID, "   ")

    query_text = str(query)
    assert "lower(company_provider_credentials.provider)" not in query_text


def test_admin_credential_listing_filters_by_company_name_too() -> None:
    query = credentials_router._admin_list_all_credentials_query("bismark")

    query_text = str(query)
    assert "JOIN companies" in query_text
    assert "lower(companies.name)" in query_text


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


def test_moabits_test_endpoint_uses_child_company_discovery_without_persisting(
    monkeypatch,
) -> None:
    db = _Db([])
    provider = _Provider(fail=True)
    captured: dict[str, Any] = {}

    async def _fetch_child_companies(credentials: dict[str, Any]) -> list[dict[str, Any]]:
        captured.update(credentials)
        return [{"companyCode": "48123", "companyName": "Bismark Colombia"}]

    monkeypatch.setattr(
        credentials_router,
        "fetch_child_companies",
        _fetch_child_companies,
    )
    client = _client(AppRole.admin, db, provider)

    response = client.post(
        "/v1/companies/me/credentials/moabits/test",
        json={
            "credentials": {
                "base_url": "https://www.api.myorion.co",
                "x_api_key": "new-key",
                "parent_company_code": "48123",
            }
        },
    )

    assert response.status_code == 200
    assert response.json() == {"provider": "moabits", "ok": True, "detail": None}
    assert db.rows == []
    assert provider.calls == []
    assert captured["company_id"] == str(COMPANY_ID)
    assert captured["x_api_key"] == "new-key"
    assert captured["parent_company_code"] == "48123"


def test_moabits_test_endpoint_returns_discovery_failure(monkeypatch) -> None:
    db = _Db([])

    async def _fetch_child_companies(credentials: dict[str, Any]) -> list[dict[str, Any]]:
        raise ProviderAuthFailed(detail="Moabits x-api-key is cancelled")

    monkeypatch.setattr(
        credentials_router,
        "fetch_child_companies",
        _fetch_child_companies,
    )
    client = _client(AppRole.admin, db)

    response = client.post(
        "/v1/companies/me/credentials/moabits/test",
        json={
            "credentials": {
                "base_url": "https://www.api.myorion.co",
                "x_api_key": "bad-key",
            }
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "moabits",
        "ok": False,
        "detail": "Moabits x-api-key is cancelled",
    }
    assert db.rows == []


def test_patch_updates_existing_active_credential_and_encrypts_credentials() -> None:
    old = _row()
    db = _Db([old])
    provider = _Provider()
    client = _client(AppRole.admin, db, provider)

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
    assert old.active is True
    assert len(db.rows) == 1
    created = db.rows[0]
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


def test_patch_creates_credential_when_provider_is_missing() -> None:
    db = _Db([])
    client = _client(AppRole.admin, db)

    response = client.patch(
        "/v1/companies/me/credentials/tele2",
        json={
            "credentials": {"username": "alice", "api_key": "new-secret"},
            "account_scope": {"account_id": "acct-2"},
        },
    )

    assert response.status_code == 200
    assert len(db.rows) == 1
    assert db.rows[0].provider == "tele2"
    assert db.rows[0].active is True
    assert response.json()["active"] is True


def test_get_credential_returns_404_when_provider_is_missing() -> None:
    client = _client(AppRole.manager, _Db([]))

    response = client.get("/v1/companies/me/credentials/tele2")

    assert response.status_code == 404
    assert response.json()["detail"] == "Credential not found"


def test_unknown_provider_is_rejected() -> None:
    client = _client(AppRole.manager, _Db([]))

    response = client.get("/v1/companies/me/credentials/unknown")

    assert response.status_code == 422


def test_patch_allows_custom_tele2_cobrand_url() -> None:
    db = _Db([])
    provider = _Provider()
    client = _client(AppRole.admin, db, provider)

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


def test_admin_patch_merges_only_sent_fields() -> None:
    old = _row(
        credentials={
            "base_url": "https://custom.jasper.example",
            "username": "alice",
            "api_key": "old-secret",
        },
        account_scope={"account_id": "acct-1", "max_tps": 3},
    )
    db = _Db([old])
    provider = _Provider()
    client = _client(AppRole.admin, db, provider)

    response = client.patch(
        "/v1/companies/me/credentials/tele2",
        json={
            "credentials": {"api_key": "new-secret"},
            "account_scope": {"max_tps": 5},
        },
    )

    assert response.status_code == 200
    decrypted = decrypt_credentials(old.credentials_enc, FERNET_KEY)
    assert decrypted["base_url"] == "https://custom.jasper.example"
    assert decrypted["username"] == "alice"
    assert decrypted["api_key"] == "new-secret"
    assert old.account_scope == {"account_id": "acct-1", "max_tps": 5}
    assert provider.calls[0]["username"] == "alice"
    assert provider.calls[0]["api_key"] == "new-secret"
    assert provider.calls[0]["max_tps"] == 5


def test_manager_can_update_moabits_x_api_key_with_mapping(monkeypatch) -> None:
    old = _row(
        provider="moabits",
        credentials={
            "base_url": "https://www.api.myorion.co",
            "x_api_key": "old-key",
            "parent_company_code": "48123",
        },
        account_scope={"environment": "production"},
    )
    db = _Db([old], moabits_mapping=_moabits_mapping())
    provider = _Provider()
    client = _client(AppRole.manager, db, provider)
    discovered: list[dict[str, Any]] = []

    async def _fetch_child_companies(credentials: dict[str, Any]) -> list[dict[str, Any]]:
        discovered.append(credentials)
        return [{"companyCode": "48123", "companyName": "Bismark Colombia"}]

    monkeypatch.setattr(
        credentials_router,
        "fetch_child_companies",
        _fetch_child_companies,
    )

    response = client.patch(
        "/v1/companies/me/credentials/moabits",
        json={"credentials": {"x_api_key": "new-key"}},
    )

    assert response.status_code == 200
    decrypted = decrypt_credentials(old.credentials_enc, FERNET_KEY)
    assert decrypted == {
        "base_url": "https://www.api.myorion.co",
        "x_api_key": "new-key",
        "parent_company_code": "48123",
    }
    assert old.account_scope == {"environment": "production"}
    assert discovered[0]["x_api_key"] == "new-key"
    assert "new-key" not in response.text


def test_manager_cannot_update_moabits_extra_fields() -> None:
    old = _row(
        provider="moabits",
        credentials={"base_url": "https://www.api.myorion.co", "x_api_key": "old-key"},
    )
    db = _Db([old], moabits_mapping=_moabits_mapping())
    client = _client(AppRole.manager, db)

    response = client.patch(
        "/v1/companies/me/credentials/moabits",
        json={"credentials": {"x_api_key": "new-key", "base_url": "https://other.example"}},
    )

    assert response.status_code == 403
    assert db.commits == 0


def test_manager_cannot_rotate_moabits_credential_without_company_mapping() -> None:
    old = _row(
        provider="moabits",
        credentials={
            "base_url": "https://www.api.myorion.co",
            "x_api_key": "old-key",
            "parent_company_code": "48123",
        },
    )
    db = _Db([old])  # no moabits_mapping
    client = _client(AppRole.manager, db)

    response = client.patch(
        "/v1/companies/me/credentials/moabits",
        json={"credentials": {"x_api_key": "new-key"}},
    )

    assert response.status_code == 403
    assert db.commits == 0


def test_manager_can_update_tele2_credential() -> None:
    old = _row(
        provider="tele2",
        credentials={"username": "alice", "api_key": "old-secret"},
    )
    db = _Db([old])
    provider = _Provider()
    client = _client(AppRole.manager, db, provider)

    response = client.patch(
        "/v1/companies/me/credentials/tele2",
        json={"credentials": {"api_key": "new-secret"}},
    )

    assert response.status_code == 200
    decrypted = decrypt_credentials(old.credentials_enc, FERNET_KEY)
    assert decrypted["api_key"] == "new-secret"
    assert decrypted["username"] == "alice"
    assert "new-secret" not in response.text


def test_patch_kite_requires_certificate_credentials() -> None:
    db = _Db([])
    provider = _Provider()
    client = _client(AppRole.admin, db, provider)

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
    client = _client(AppRole.admin, db, provider)
    expires_at = datetime(2026, 12, 31, tzinfo=UTC)
    pfx_b64 = _kite_pfx_b64(expires_at)

    response = client.patch(
        "/v1/companies/me/credentials/kite",
        json={
            "credentials": {
                "endpoint": "https://kite.test/soap/",
                "client_cert_pfx_b64": pfx_b64,
                "client_cert_password": "pfx-secret",
                "server_ca_cert_pem_b64": "BASE64-CA",
            },
            "account_scope": {
                "environment": "production",
                "end_customer_id": "end-customer-1",
                "cert_expires_at": "2000-01-01T00:00:00Z",
            },
        },
    )

    assert response.status_code == 200
    assert len(db.rows) == 1
    decrypted = decrypt_credentials(db.rows[-1].credentials_enc, FERNET_KEY)
    assert decrypted["endpoint"] == "https://kite.test/soap"
    assert decrypted["client_cert_pfx_b64"] == pfx_b64
    assert decrypted["client_cert_password"] == "pfx-secret"
    assert decrypted["server_ca_bundle_pem_b64"] == "BASE64-CA"
    assert "server_ca_cert_pem_b64" not in decrypted
    assert decrypted["end_customer_id"] == "end-customer-1"
    assert db.rows[-1].account_scope["cert_expires_at"] == "2026-12-31T00:00:00Z"
    assert provider.calls[0]["company_id"] == str(COMPANY_ID)
    assert provider.calls[0]["client_cert_pfx_b64"] == pfx_b64
    assert provider.calls[0]["server_ca_bundle_pem_b64"] == "BASE64-CA"
    assert pfx_b64 not in response.text
    assert "pfx-secret" not in response.text


def test_patch_kite_always_refreshes_certificate_expiry_from_existing_pfx() -> None:
    expires_at = datetime(2027, 1, 15, 12, 30, tzinfo=UTC)
    pfx_b64 = _kite_pfx_b64(expires_at)
    old = _row(
        provider="kite",
        credentials={
            "endpoint": "https://kite.test/soap",
            "client_cert_pfx_b64": pfx_b64,
            "client_cert_password": "pfx-secret",
        },
        account_scope={
            "environment": "staging",
            "cert_expires_at": "2000-01-01T00:00:00Z",
        },
    )
    db = _Db([old])
    client = _client(AppRole.admin, db, _Provider())

    response = client.patch(
        "/v1/companies/me/credentials/kite",
        json={
            "account_scope": {
                "environment": "production",
                "cert_expires_at": "2001-01-01T00:00:00Z",
            },
        },
    )

    assert response.status_code == 200
    assert old.account_scope == {
        "environment": "production",
        "cert_expires_at": "2027-01-15T12:30:00Z",
    }


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


def test_admin_can_list_all_credentials_with_company_scope_without_secrets() -> None:
    db = _Db([_row(account_scope={"account_id": "acct-1"})])
    client = _client(AppRole.admin, db)

    response = client.get("/v1/admin/credentials")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["company_id"] == str(COMPANY_ID)
    assert payload[0]["provider"] == "tele2"
    assert payload[0]["account_scope"] == {"account_id": "acct-1"}
    assert "old-secret" not in response.text
    assert "credentials_enc" not in response.text
    assert "api_key" not in response.text


def test_manager_cannot_use_admin_credentials_scope() -> None:
    client = _client(AppRole.manager, _Db([_row()]))

    response = client.get("/v1/admin/credentials")

    assert response.status_code == 403


def test_admin_can_list_company_credentials() -> None:
    db = _Db([_row(provider="kite"), _row(provider="tele2")])
    client = _client(AppRole.admin, db)

    response = client.get(f"/v1/admin/companies/{COMPANY_ID}/credentials")

    assert response.status_code == 200
    assert [item["provider"] for item in response.json()] == ["kite", "tele2"]


def test_admin_patch_company_credential_uses_company_id_from_path() -> None:
    target_company_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
    db = _Db([])
    db.company.id = target_company_id
    provider = _Provider()
    client = _client(AppRole.admin, db, provider)

    response = client.patch(
        f"/v1/admin/companies/{target_company_id}/credentials/tele2",
        json={
            "credentials": {"username": "alice", "api_key": "new-secret"},
            "account_scope": {"account_id": "acct-admin"},
        },
    )

    assert response.status_code == 200
    assert response.json()["company_id"] == str(target_company_id)
    assert len(db.rows) == 1
    assert db.rows[0].company_id == target_company_id
    assert provider.calls[0]["company_id"] == str(target_company_id)
    assert "new-secret" not in response.text


def test_admin_can_test_company_credential_without_persisting() -> None:
    provider = _Provider()
    db = _Db([])
    client = _client(AppRole.admin, db, provider)

    response = client.post(
        f"/v1/admin/companies/{COMPANY_ID}/credentials/tele2/test",
        json={"credentials": {"username": "alice", "api_key": "new-secret"}},
    )

    assert response.status_code == 200
    assert response.json() == {"provider": "tele2", "ok": True, "detail": None}
    assert db.rows == []
    assert provider.calls[0]["company_id"] == str(COMPANY_ID)


def test_admin_delete_company_credential_deactivates_target_provider() -> None:
    row = _row(provider="tele2")
    db = _Db([row])
    client = _client(AppRole.admin, db)

    response = client.delete(f"/v1/admin/companies/{COMPANY_ID}/credentials/tele2")

    assert response.status_code == 204
    assert row.active is False


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
        assert "company_codes" not in examples["moabits"]["value"]["credentials"]
