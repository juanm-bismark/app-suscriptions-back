import uuid
from datetime import UTC, datetime

from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_pagination import Page
from fastapi_pagination.api import add_pagination

from app.config import Settings, get_settings
from app.database import get_db
from app.identity.dependencies import get_current_profile
from app.identity.models.profile import AppRole, Profile
from app.shared.crypto import encrypt_credentials
from app.tenancy.models.company import Company
from app.tenancy.models.credentials import CompanyProviderCredentials
from app.tenancy.models.moabits_source_company import MoabitsSourceCompany
from app.tenancy.models.provider_mapping import CompanyProviderMapping
from app.tenancy.routers import companies

COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
FERNET_KEY = Fernet.generate_key().decode()


class _ScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _ListResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Db:
    def __init__(self, duplicate_company_id: uuid.UUID | None = None) -> None:
        self.company = Company(
            id=COMPANY_ID,
            name="Bismark",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.duplicate_company_id = duplicate_company_id
        self.added = []
        self.committed = False

    async def execute(self, statement):
        statement_text = str(statement)
        if (
            "FROM companies" in statement_text
            and statement_text.splitlines()[0].strip() == "SELECT companies.id"
        ):
            return _ScalarResult(self.duplicate_company_id)
        return _ScalarResult(self.company)

    def add(self, row):
        self.added.append(row)
        if isinstance(row, Company):
            self.company = row

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.committed = False

    async def refresh(self, row):
        if isinstance(row, Company) and row.created_at is None:
            row.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        return None


async def _db_override():
    yield _Db()


def _profile(role: AppRole) -> Profile:
    return Profile(id=USER_ID, company_id=COMPANY_ID, role=role)


def _moabits_credential() -> CompanyProviderCredentials:
    return CompanyProviderCredentials(
        id=uuid.uuid4(),
        company_id=COMPANY_ID,
        provider="moabits",
        credentials_enc=encrypt_credentials(
            {
                "base_url": "https://www.api.myorion.co",
                "x_api_key": "secret-key",
                "parent_company_code": "48123",
            },
            FERNET_KEY,
        ),
        account_scope={},
        active=True,
        rotated_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _client(role: AppRole, db=None) -> TestClient:
    app = FastAPI()
    app.include_router(companies.router, prefix="/v1")
    app.dependency_overrides[get_current_profile] = lambda: _profile(role)
    app.dependency_overrides[get_settings] = lambda: Settings(fernet_key=FERNET_KEY)
    if db is None:
        app.dependency_overrides[get_db] = _db_override
    else:

        async def _custom_db_override():
            yield db

        app.dependency_overrides[get_db] = _custom_db_override
    add_pagination(app)
    return TestClient(app)


def test_admin_can_list_companies(monkeypatch) -> None:
    async def _apaginate(_db, _query, params):
        row = Company(
            id=COMPANY_ID, name="Bismark", created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        return Page.create([row], params, total=1)

    monkeypatch.setattr(companies, "apaginate", _apaginate)

    response = _client(AppRole.admin).get("/v1/companies?page=1&size=20")

    assert response.status_code == 200
    assert response.json()["items"][0]["name"] == "Bismark"


def test_company_listing_filters_by_name_when_q_is_present() -> None:
    query = companies._list_companies_query("bis")

    query_text = str(query)
    assert "lower(companies.name)" in query_text


def test_company_listing_ignores_blank_q() -> None:
    query = companies._list_companies_query("   ")

    query_text = str(query)
    assert "lower(companies.name)" not in query_text


def test_manager_cannot_list_companies() -> None:
    response = _client(AppRole.manager).get("/v1/companies?page=1&size=20")

    assert response.status_code == 403


def test_admin_can_create_company() -> None:
    response = _client(AppRole.admin).post("/v1/companies", json={"name": "New"})

    assert response.status_code == 201
    assert response.json()["name"] == "New"


def test_admin_create_company_trims_name() -> None:
    response = _client(AppRole.admin).post(
        "/v1/companies", json={"name": "  New  "}
    )

    assert response.status_code == 201
    assert response.json()["name"] == "New"


def test_admin_create_company_rejects_blank_name() -> None:
    db = _Db()

    response = _client(AppRole.admin, db).post(
        "/v1/companies", json={"name": "  "}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "name cannot be empty"
    assert db.added == []
    assert db.committed is False


def test_admin_create_company_rejects_duplicate_name_before_insert() -> None:
    duplicate_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    db = _Db(duplicate_company_id=duplicate_id)

    response = _client(AppRole.admin, db).post(
        "/v1/companies", json={"name": "Existing"}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Company name already exists"
    assert db.added == []
    assert db.committed is False


def test_member_can_read_company() -> None:
    response = _client(AppRole.member).get("/v1/companies/me")

    assert response.status_code == 200
    assert response.json()["name"] == "Bismark"


def test_public_cannot_read_company() -> None:
    response = _client(AppRole.public).get("/v1/companies/me")

    assert response.status_code == 403


def test_manager_cannot_update_company() -> None:
    response = _client(AppRole.manager).put("/v1/companies/me", json={"name": "New"})

    assert response.status_code == 403


def test_admin_can_update_company() -> None:
    response = _client(AppRole.admin).put("/v1/companies/me", json={"name": "New"})

    assert response.status_code == 200
    assert response.json()["name"] == "New"


def test_admin_update_company_rejects_blank_name() -> None:
    db = _Db()

    response = _client(AppRole.admin, db).put(
        "/v1/companies/me", json={"name": "  "}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "name cannot be empty"
    assert db.company.name == "Bismark"
    assert db.committed is False


def test_admin_update_company_rejects_duplicate_name_before_overwriting() -> None:
    duplicate_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    db = _Db(duplicate_company_id=duplicate_id)

    response = _client(AppRole.admin, db).put(
        "/v1/companies/me", json={"name": "Existing"}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Company name already exists"
    assert db.company.name == "Bismark"
    assert db.committed is False


def test_admin_update_company_allows_keeping_current_name() -> None:
    duplicate_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    db = _Db(duplicate_company_id=duplicate_id)

    response = _client(AppRole.admin, db).put(
        "/v1/companies/me", json={"name": " bismark "}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "bismark"
    assert db.committed is True


def test_admin_can_read_company_by_id() -> None:
    response = _client(AppRole.admin).get(f"/v1/companies/{COMPANY_ID}")

    assert response.status_code == 200
    assert response.json()["id"] == str(COMPANY_ID)


def test_admin_can_update_company_by_id() -> None:
    response = _client(AppRole.admin).put(
        f"/v1/companies/{COMPANY_ID}", json={"name": "New"}
    )

    assert response.status_code == 200
    assert response.json()["name"] == "New"


class _MappingDb:
    def __init__(
        self,
        *,
        credential: CompanyProviderCredentials | None = None,
        mapping: CompanyProviderMapping | None = None,
    ) -> None:
        self.company = Company(
            id=COMPANY_ID,
            name="Bismark",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.credential = credential
        self.mapping = mapping
        self.added = []
        self.commits = 0
        self.execute_calls = 0

    async def execute(self, statement):
        self.execute_calls += 1
        statement_text = str(statement)
        if "FROM companies" in statement_text:
            return _ScalarResult(self.company)
        if "FROM company_provider_credentials" in statement_text:
            return _ScalarResult(self.credential)
        if "FROM moabits_source_companies" in statement_text:
            return _ScalarResult(None)
        if "FROM company_provider_mappings" in statement_text:
            return _ScalarResult(self.mapping)
        return _ScalarResult(self.mapping)

    def add(self, row):
        self.added.append(row)
        if isinstance(row, CompanyProviderMapping):
            self.mapping = row

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def refresh(self, row):
        if isinstance(row, CompanyProviderMapping):
            row.created_at = row.created_at or datetime(2026, 1, 1, tzinfo=UTC)
            row.updated_at = row.updated_at or datetime(2026, 1, 1, tzinfo=UTC)


def test_admin_can_upsert_moabits_provider_mapping(monkeypatch) -> None:
    async def _children(credentials: dict) -> list[dict]:
        assert credentials["x_api_key"] == "secret-key"
        return [
            {
                "companyCode": "48123-99",
                "companyName": "3 POINTECH S.A.S.",
                "clie_id": 2659,
            }
        ]

    monkeypatch.setattr(companies, "fetch_child_companies", _children)
    db = _MappingDb(credential=_moabits_credential())

    response = _client(AppRole.admin, db).put(
        f"/v1/companies/{COMPANY_ID}/provider-mappings/moabits",
        json={
            "companyCode": "48123-99",
            "companyName": "3 POINTECH S.A.S.",
            "clie_id": 2659,
        },
    )

    assert response.status_code == 200
    assert response.json()["companyCode"] == "48123-99"
    assert response.json()["companyName"] == "3 POINTECH S.A.S."
    assert db.mapping is not None
    assert db.mapping.company_id == COMPANY_ID
    assert db.mapping.provider == "moabits"
    assert db.mapping.provider_company_code == "48123-99"
    assert db.mapping.settings == {}
    assert db.commits == 1


def test_admin_can_upsert_moabits_mapping_from_live_discovery(monkeypatch) -> None:
    async def _children(credentials: dict) -> list[dict]:
        assert credentials["x_api_key"] == "secret-key"
        return [
            {
                "companyCode": "48123-99",
                "companyName": "3 POINTECH S.A.S.",
                "clie_id": 2659,
            }
        ]

    monkeypatch.setattr(companies, "fetch_child_companies", _children)
    db = _MappingDb(credential=_moabits_credential())

    response = _client(AppRole.admin, db).put(
        f"/v1/companies/{COMPANY_ID}/provider-mappings/moabits",
        json={"companyCode": "48123-99"},
    )

    assert response.status_code == 200
    assert response.json()["companyCode"] == "48123-99"
    assert response.json()["companyName"] == "3 POINTECH S.A.S."
    assert response.json()["clie_id"] == 2659
    assert db.mapping is not None
    assert db.mapping.provider_company_code == "48123-99"
    assert db.commits == 1


def test_moabits_provider_mapping_code_must_exist_in_moabits_api(monkeypatch) -> None:
    async def _children(credentials: dict) -> list[dict]:
        assert credentials["x_api_key"] == "secret-key"
        return [{"companyCode": "48123", "companyName": "Bismark Colombia"}]

    monkeypatch.setattr(companies, "fetch_child_companies", _children)
    db = _MappingDb(credential=_moabits_credential())

    response = _client(AppRole.admin, db).put(
        f"/v1/companies/{COMPANY_ID}/provider-mappings/moabits",
        json={"companyCode": "48123-99"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["company_code"] == "48123-99"
    assert db.mapping is None
    assert db.commits == 0


def test_admin_can_discover_moabits_mapping_options(monkeypatch) -> None:
    other_company_id = uuid.UUID("00000000-0000-0000-0000-000000000003")

    async def _children(credentials: dict) -> list[dict]:
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

    monkeypatch.setattr(companies, "fetch_child_companies", _children)

    local_companies = [
        Company(
            id=other_company_id,
            name="3 Pointech",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        Company(
            id=COMPANY_ID,
            name="Bismark",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]
    mapping = CompanyProviderMapping(
        company_id=COMPANY_ID,
        provider="moabits",
        provider_company_code="48123",
        provider_company_name="Bismark Colombia",
        clie_id=132,
        settings={},
        active=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    stale_source_company = MoabitsSourceCompany(
        source_company_id=COMPANY_ID,
        company_code="stale-code",
        company_name="Old cached row",
        clie_id=None,
        raw_payload={},
        active=True,
        last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    class _DiscoveryDb:
        def __init__(self) -> None:
            self.source_companies = []
            self.commits = 0

        async def execute(self, statement):
            statement_text = str(statement)
            if "FROM company_provider_credentials" in statement_text:
                return _ListResult([_moabits_credential()])
            if "FROM company_provider_mappings" in statement_text:
                return _ListResult([mapping])
            if "FROM moabits_source_companies" in statement_text:
                return _ListResult([stale_source_company])
            if "FROM companies" in statement_text:
                return _ListResult(local_companies)
            return _ListResult([])

        def add(self, row):
            if isinstance(row, MoabitsSourceCompany):
                self.source_companies.append(row)

        async def commit(self):
            self.commits += 1

    db = _DiscoveryDb()
    response = _client(AppRole.admin, db).get(
        "/v1/companies/provider-mappings/moabits/discover"
    )

    assert response.status_code == 200
    payload = response.json()
    assert "refreshes the cached Moabits source companies" in payload["cache_message"]
    assert payload["source_company_codes"] == ["48123", "48123-99"]
    assert [row.company_code for row in db.source_companies] == ["48123-99", "48123"]
    assert stale_source_company.active is False
    assert db.commits == 1
    assert payload["local_companies"][0]["company_id"] == str(other_company_id)
    assert payload["local_companies"][1]["company_id"] == str(COMPANY_ID)
    linked_moabits = next(
        item for item in payload["moabits_companies"] if item["companyCode"] == "48123"
    )
    assert linked_moabits["selected_in_source"] is True
    assert linked_moabits["linked_companies"] == [
        {"company_id": str(COMPANY_ID), "company_name": "Bismark"}
    ]
    unlinked_moabits = next(
        item
        for item in payload["moabits_companies"]
        if item["companyCode"] == "48123-99"
    )
    assert unlinked_moabits["selected_in_source"] is True
    assert unlinked_moabits["linked_companies"] == []


def _mapping_list_db(local_companies, mapping=None):
    class _MappingListDb:
        async def execute(self, statement):
            statement_text = str(statement)
            if "FROM company_provider_credentials" in statement_text:
                raise AssertionError("list endpoint must not load credentials")
            if "FROM company_provider_mappings" in statement_text:
                return _ListResult([mapping] if mapping else [])
            if "FROM companies" in statement_text:
                return _ListResult(local_companies)
            return _ListResult([])

    return _MappingListDb()


def test_admin_can_list_cached_moabits_source_companies() -> None:
    source_company = MoabitsSourceCompany(
        source_company_id=COMPANY_ID,
        company_code="48123",
        company_name="Bismark Colombia",
        clie_id=132,
        raw_payload={"companyCode": "48123"},
        active=True,
        last_seen_at=datetime(2026, 1, 2, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )

    class _SourceCompaniesDb:
        async def execute(self, statement):
            statement_text = str(statement)
            if "FROM company_provider_mappings" in statement_text:
                raise AssertionError("source companies endpoint must not read mappings")
            if "FROM moabits_source_companies" in statement_text:
                return _ListResult([source_company])
            return _ListResult([])

    response = _client(AppRole.admin, _SourceCompaniesDb()).get(
        "/v1/companies/provider-mappings/moabits/source-companies?page=1&size=20"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["companyCode"] == "48123"
    assert payload["items"][0]["companyName"] == "Bismark Colombia"
    assert payload["items"][0]["clie_id"] == 132


def test_admin_can_list_moabits_mappings_without_live_discovery(monkeypatch) -> None:
    async def _children(_credentials: dict) -> list[dict]:
        raise AssertionError("list endpoint must not call Moabits discovery")

    monkeypatch.setattr(companies, "fetch_child_companies", _children)
    other_company_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    local_companies = [
        Company(
            id=other_company_id,
            name="3 Pointech",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        Company(
            id=COMPANY_ID,
            name="Bismark",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]
    mapping = CompanyProviderMapping(
        company_id=COMPANY_ID,
        provider="moabits",
        provider_company_code="48123",
        provider_company_name="Bismark Colombia",
        clie_id=132,
        settings={},
        active=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    response = _client(AppRole.admin, _mapping_list_db(local_companies, mapping)).get(
        "/v1/companies/provider-mappings/moabits?page=1&size=20"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    bismark = next(
        item for item in payload["items"] if item["company_id"] == str(COMPANY_ID)
    )
    assert bismark["company_name"] == "Bismark"
    assert bismark["mapping"]["companyCode"] == "48123"
    assert bismark["mapping"]["companyName"] == "Bismark Colombia"
    unlinked = next(
        item for item in payload["items"] if item["company_id"] == str(other_company_id)
    )
    assert unlinked["mapping"] is None


def test_moabits_mappings_linked_only_excludes_unlinked_companies() -> None:
    other_company_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    local_companies = [
        Company(
            id=other_company_id,
            name="3 Pointech",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        Company(
            id=COMPANY_ID,
            name="Bismark",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]
    mapping = CompanyProviderMapping(
        company_id=COMPANY_ID,
        provider="moabits",
        provider_company_code="48123",
        provider_company_name="Bismark Colombia",
        clie_id=132,
        settings={},
        active=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    response = _client(AppRole.admin, _mapping_list_db(local_companies, mapping)).get(
        "/v1/companies/provider-mappings/moabits?page=1&size=20&linked_only=true"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["company_id"] == str(COMPANY_ID)
    assert payload["items"][0]["mapping"]["companyCode"] == "48123"


def test_moabits_mappings_q_filters_by_company_name() -> None:
    other_company_id = uuid.UUID("00000000-0000-0000-0000-000000000003")
    local_companies = [
        Company(
            id=other_company_id,
            name="3 Pointech",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
        Company(
            id=COMPANY_ID,
            name="Bismark",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]

    response = _client(AppRole.admin, _mapping_list_db(local_companies)).get(
        "/v1/companies/provider-mappings/moabits?page=1&size=20&q=bismark"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["company_name"] == "Bismark"


def test_moabits_mappings_q_filters_by_provider_code() -> None:
    local_companies = [
        Company(
            id=COMPANY_ID,
            name="Bismark",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    ]
    mapping = CompanyProviderMapping(
        company_id=COMPANY_ID,
        provider="moabits",
        provider_company_code="48123",
        provider_company_name="Bismark Colombia",
        clie_id=132,
        settings={},
        active=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    response = _client(AppRole.admin, _mapping_list_db(local_companies, mapping)).get(
        "/v1/companies/provider-mappings/moabits?page=1&size=20&q=48123"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["mapping"]["companyCode"] == "48123"


def test_manager_can_read_own_company_provider_mapping() -> None:
    mapping = CompanyProviderMapping(
        company_id=COMPANY_ID,
        provider="moabits",
        provider_company_code="48123",
        provider_company_name="Bismark Colombia",
        clie_id=132,
        settings={},
        active=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db = _MappingDb(mapping=mapping)

    response = _client(AppRole.manager, db).get(
        "/v1/companies/me/provider-mappings/moabits"
    )

    assert response.status_code == 200
    assert response.json()["companyCode"] == "48123"
    assert response.json()["companyName"] == "Bismark Colombia"
    assert response.json()["clie_id"] == 132


def test_manager_gets_404_when_own_company_has_no_mapping() -> None:
    db = _MappingDb()

    response = _client(AppRole.manager, db).get(
        "/v1/companies/me/provider-mappings/moabits"
    )

    assert response.status_code == 404


def test_member_cannot_read_own_company_provider_mapping() -> None:
    db = _MappingDb()

    response = _client(AppRole.member, db).get(
        "/v1/companies/me/provider-mappings/moabits"
    )

    assert response.status_code == 403


def test_company_provider_mapping_get_by_company_endpoint_is_removed() -> None:
    db = _MappingDb()

    response = _client(AppRole.admin, db).get(
        f"/v1/companies/{COMPANY_ID}/provider-mappings/moabits"
    )

    assert response.status_code == 405
    assert db.commits == 0


def test_manager_cannot_upsert_moabits_provider_mapping() -> None:
    db = _MappingDb()

    response = _client(AppRole.manager, db).put(
        f"/v1/companies/{COMPANY_ID}/provider-mappings/moabits",
        json={"companyCode": "48123"},
    )

    assert response.status_code == 403
    assert db.commits == 0
