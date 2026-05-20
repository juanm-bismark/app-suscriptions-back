import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.database import get_db
from app.identity.dependencies import get_current_profile
from app.identity.models.profile import AppRole, Profile
from app.providers.registry import ProviderRegistry
from app.shared.crypto import encrypt_credentials
from app.shared.errors import DomainError
from app.subscriptions.domain import AdministrativeStatus, Subscription
from app.subscriptions.routers import sims
from app.subscriptions.schemas.sim import SimListOut
from app.tenancy.models.credentials import CompanyProviderCredentials
from app.tenancy.models.provider_mapping import CompanyProviderMapping

COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class _ScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalars(self):
        return self

    def all(self):
        if self._row is None:
            return []
        if isinstance(self._row, list):
            return self._row
        return [self._row]


class _Routing:
    provider = "tele2"


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    async def purge(self, iccid, credentials, *, idempotency_key):
        self.calls += 1


class _Registry:
    def __init__(self, provider: _Provider) -> None:
        self.provider = provider

    def get(self, provider: str):
        return self.provider


async def _db_override():
    yield object()


def _profile(role: AppRole) -> Profile:
    return Profile(id=USER_ID, company_id=COMPANY_ID, role=role)


def _client(
    role: AppRole, registry: ProviderRegistry | _Registry | None = None
) -> TestClient:
    app = FastAPI()

    @app.exception_handler(DomainError)
    async def _domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"code": exc.code, "detail": exc.detail, **exc.extra},
        )

    app.include_router(sims.router, prefix="/v1")
    app.dependency_overrides[get_current_profile] = lambda: _profile(role)
    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_settings] = lambda: Settings()
    app.dependency_overrides[sims.get_registry] = lambda: (
        registry or _Registry(_Provider())
    )
    return TestClient(app)


def test_purge_requires_idempotency_key() -> None:
    client = _client(AppRole.admin)

    response = client.post("/v1/sims/8934070100000000001/purge")

    assert response.status_code == 400
    assert response.json()["code"] == "request.idempotency_key_required"


def test_purge_requires_admin_role() -> None:
    client = _client(AppRole.manager)

    response = client.post(
        "/v1/sims/8934070100000000001/purge",
        headers={"Idempotency-Key": "same-key"},
    )

    assert response.status_code == 403


def test_purge_replay_does_not_call_provider(monkeypatch) -> None:
    provider = _Provider()
    client = _client(AppRole.admin, _Registry(provider))

    async def _resolve(*args, **kwargs):
        return _Routing()

    async def _claim(*args, **kwargs):
        return False

    async def _audit(*args, **kwargs):
        return None

    monkeypatch.setattr(sims, "_resolve_routing", _resolve)
    monkeypatch.setattr(sims, "_claim_idempotency_key", _claim)
    monkeypatch.setattr(sims, "_write_lifecycle_audit", _audit)

    response = client.post(
        "/v1/sims/8934070100000000001/purge",
        headers={"Idempotency-Key": "same-key"},
    )

    assert response.status_code == 204
    assert provider.calls == 0


def test_global_listing_rejects_filters_without_provider() -> None:
    client = _client(AppRole.member)

    response = client.get("/v1/sims?status=active")

    assert response.status_code == 409
    assert response.json()["code"] == "provider.unsupported_operation"


def test_tele2_bootstrap_filters_include_default_modified_since() -> None:
    filters = sims._bootstrap_filters_for_provider("tele2")

    assert filters.modified_since is not None
    assert filters.modified_since.tzinfo == UTC


@pytest.mark.asyncio
async def test_global_listing_bootstraps_empty_routing_map(monkeypatch) -> None:
    calls: list[tuple[str, int]] = []

    class _Db:
        def __init__(self) -> None:
            self.commit_calls = 0

        async def execute(self, stmt):
            return None

        async def commit(self):
            self.commit_calls += 1

    class _SearchProvider:
        def __init__(self, provider):
            self.provider = provider

        async def list_subscriptions(self, credentials, *, cursor, limit, filters):
            calls.append((self.provider, limit))
            return (
                [
                    Subscription(
                        iccid=f"iccid-{self.provider}",
                        msisdn=None,
                        imsi=None,
                        status=AdministrativeStatus.ACTIVE,
                        native_status="active",
                        provider=self.provider,
                        company_id=str(COMPANY_ID),
                        activated_at=None,
                        updated_at=None,
                    )
                ],
                None,
            )

        async def get_subscription(self, iccid, credentials):
            raise AssertionError("empty page rows should not fetch details")

    class _SearchRegistry:
        def get(self, provider):
            return _SearchProvider(provider)

    async def _credentials(*args, **kwargs):
        return {"company_code": "48123"}

    async def _upsert(*args, **kwargs):
        return None

    monkeypatch.setattr(sims, "_load_credentials", _credentials)
    monkeypatch.setattr(sims, "_upsert_routing", _upsert)
    db = _Db()

    result = await sims._list_via_routing_index(
        cursor=None,
        limit=50,
        filters=sims._build_filters(
            status_filter=None,
            modified_since=None,
            modified_till=None,
            iccid=None,
            imsi=None,
            msisdn=None,
            custom=None,
        ),
        company_id=COMPANY_ID,
        db=db,
        settings=Settings(),
        registry=_SearchRegistry(),
    )

    assert calls == [("kite", 17), ("tele2", 17), ("moabits", 16)]
    assert db.commit_calls == 1
    assert result.total is None
    assert [item.provider for item in result.items] == ["kite", "tele2", "moabits"]
    assert [
        (status.provider, status.status, status.count)
        for status in result.provider_statuses
    ] == [
        ("kite", "ok", 1),
        ("tele2", "ok", 1),
        ("moabits", "ok", 1),
    ]


@pytest.mark.asyncio
async def test_global_listing_uses_provider_summaries_without_detail_calls(
    monkeypatch,
) -> None:
    calls: list[tuple[str, int]] = []

    class _Db:
        def __init__(self) -> None:
            self.commit_calls = 0

        async def execute(self, stmt):
            return None

        async def commit(self):
            self.commit_calls += 1

    class _SearchProvider:
        def __init__(self, provider):
            self.provider = provider

        async def list_subscriptions(self, credentials, *, cursor, limit, filters):
            calls.append((self.provider, limit))
            return (
                [
                    Subscription(
                        iccid=f"iccid-{self.provider}",
                        msisdn=None,
                        imsi=None,
                        status=AdministrativeStatus.ACTIVE,
                        native_status="active",
                        provider=self.provider,
                        company_id=str(COMPANY_ID),
                        activated_at=None,
                        updated_at=None,
                    )
                ],
                None,
            )

        async def get_subscription(self, iccid, credentials):
            raise AssertionError("empty page rows should not fetch details")

    class _SearchRegistry:
        def get(self, provider):
            return _SearchProvider(provider)

    async def _credentials(*args, **kwargs):
        return {"company_code": "48123"}

    async def _upsert(*args, **kwargs):
        return None

    monkeypatch.setattr(sims, "_load_credentials", _credentials)
    monkeypatch.setattr(sims, "_upsert_routing", _upsert)
    db = _Db()

    result = await sims._list_via_routing_index(
        cursor=None,
        limit=50,
        filters=sims._build_filters(
            status_filter=None,
            modified_since=None,
            modified_till=None,
            iccid=None,
            imsi=None,
            msisdn=None,
            custom=None,
        ),
        company_id=COMPANY_ID,
        db=db,
        settings=Settings(),
        registry=_SearchRegistry(),
    )

    assert calls == [("kite", 17), ("tele2", 17), ("moabits", 16)]
    assert db.commit_calls == 1
    assert result.total is None
    assert len(result.items) == 3
    assert [
        (status.provider, status.status, status.count)
        for status in result.provider_statuses
    ] == [
        ("kite", "ok", 1),
        ("tele2", "ok", 1),
        ("moabits", "ok", 1),
    ]


@pytest.mark.asyncio
async def test_global_listing_queries_providers_concurrently(monkeypatch) -> None:
    state = {"active": 0, "max_active": 0}

    class _Db:
        async def execute(self, stmt):
            return None

        async def commit(self):
            pass

    class _SearchProvider:
        def __init__(self, provider):
            self.provider = provider

        async def list_subscriptions(self, credentials, *, cursor, limit, filters):
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
            await asyncio.sleep(0)
            state["active"] -= 1
            return [], None

    class _SearchRegistry:
        def get(self, provider):
            return _SearchProvider(provider)

    async def _credentials(*args, **kwargs):
        return {"company_code": "48123"}

    monkeypatch.setattr(sims, "_load_credentials", _credentials)

    result = await sims._list_via_routing_index(
        cursor=None,
        limit=50,
        filters=sims._build_filters(
            status_filter=None,
            modified_since=None,
            modified_till=None,
            iccid=None,
            imsi=None,
            msisdn=None,
            custom=None,
        ),
        company_id=COMPANY_ID,
        db=_Db(),
        settings=Settings(),
        registry=_SearchRegistry(),
    )

    assert state["max_active"] > 1
    assert [
        (status.provider, status.status, status.count)
        for status in result.provider_statuses
    ] == [
        ("kite", "ok", 0),
        ("tele2", "ok", 0),
        ("moabits", "ok", 0),
    ]


@pytest.mark.asyncio
async def test_global_listing_respects_total_limit_across_providers(
    monkeypatch,
) -> None:
    class _Db:
        def __init__(self) -> None:
            self.commit_calls = 0

        async def execute(self, stmt):
            return None

        async def commit(self):
            self.commit_calls += 1

    class _Provider:
        def __init__(self, provider):
            self.provider = provider

        async def list_subscriptions(self, credentials, *, cursor, limit, filters):
            return (
                [
                    Subscription(
                        iccid=f"iccid-{self.provider}-{idx:02d}",
                        msisdn=None,
                        imsi=None,
                        status=AdministrativeStatus.ACTIVE,
                        native_status="active",
                        provider=self.provider,
                        company_id=str(COMPANY_ID),
                        activated_at=None,
                        updated_at=None,
                    )
                    for idx in range(limit)
                ],
                "next" if self.provider != "moabits" else None,
            )

    class _Registry:
        def get(self, provider):
            return _Provider(provider)

    async def _credentials(*args, **kwargs):
        return {"company_code": "48123"}

    async def _upsert(*args, **kwargs):
        return None

    monkeypatch.setattr(sims, "_load_credentials", _credentials)
    monkeypatch.setattr(sims, "_upsert_routing", _upsert)
    db = _Db()

    result = await sims._list_via_routing_index(
        cursor=None,
        limit=50,
        filters=sims._build_filters(
            status_filter=None,
            modified_since=None,
            modified_till=None,
            iccid=None,
            imsi=None,
            msisdn=None,
            custom=None,
        ),
        company_id=COMPANY_ID,
        db=db,
        settings=Settings(),
        registry=_Registry(),
    )

    providers = [item.provider for item in result.items]
    assert providers.count("kite") == 17
    assert providers.count("tele2") == 17
    assert providers.count("moabits") == 16
    assert len(result.items) == 50
    assert result.next_cursor is not None
    assert result.next_cursor.startswith("global:")
    assert db.commit_calls == 1
    assert [
        (status.provider, status.status, status.count)
        for status in result.provider_statuses
    ] == [
        ("kite", "ok", 17),
        ("tele2", "ok", 17),
        ("moabits", "ok", 16),
    ]


@pytest.mark.asyncio
async def test_global_listing_carries_unqueried_providers_in_cursor(
    monkeypatch,
) -> None:
    calls: list[tuple[str, int]] = []

    class _Db:
        def __init__(self) -> None:
            self.commit_calls = 0

        async def execute(self, stmt):
            return None

        async def commit(self):
            self.commit_calls += 1

    class _Provider:
        def __init__(self, provider):
            self.provider = provider

        async def list_subscriptions(self, credentials, *, cursor, limit, filters):
            calls.append((self.provider, limit))
            return (
                [
                    Subscription(
                        iccid=f"iccid-{self.provider}",
                        msisdn=None,
                        imsi=None,
                        status=AdministrativeStatus.ACTIVE,
                        native_status="active",
                        provider=self.provider,
                        company_id=str(COMPANY_ID),
                        activated_at=None,
                        updated_at=None,
                    )
                ],
                None,
            )

    class _Registry:
        def get(self, provider):
            return _Provider(provider)

    async def _credentials(*args, **kwargs):
        return {"company_code": "48123"}

    async def _upsert(*args, **kwargs):
        return None

    monkeypatch.setattr(sims, "_load_credentials", _credentials)
    monkeypatch.setattr(sims, "_upsert_routing", _upsert)
    db = _Db()

    result = await sims._list_via_routing_index(
        cursor=None,
        limit=2,
        filters=sims._build_filters(
            status_filter=None,
            modified_since=None,
            modified_till=None,
            iccid=None,
            imsi=None,
            msisdn=None,
            custom=None,
        ),
        company_id=COMPANY_ID,
        db=db,
        settings=Settings(),
        registry=_Registry(),
    )

    assert calls == [("kite", 1), ("tele2", 1)]
    assert [item.provider for item in result.items] == ["kite", "tele2"]
    assert sims._decode_global_cursor(result.next_cursor) == {"moabits": None}
    assert [
        (status.provider, status.status, status.count)
        for status in result.provider_statuses
    ] == [
        ("kite", "ok", 1),
        ("tele2", "ok", 1),
        ("moabits", "not_queried", 0),
    ]


@pytest.mark.asyncio
async def test_load_moabits_credentials_raises_without_mapping() -> None:
    from app.shared.errors import ListingPreconditionFailed

    fernet_key = Fernet.generate_key().decode()
    credentials_row = CompanyProviderCredentials(
        company_id=COMPANY_ID,
        provider="moabits",
        credentials_enc=encrypt_credentials(
            {
                "base_url": "https://www.api.myorion.co",
                "x_api_key": "secret-key",
            },
            fernet_key,
        ),
        account_scope={},
        active=True,
        created_at=datetime.now(),
    )

    class _Db:
        async def execute(self, stmt):
            statement_text = str(stmt)
            if "FROM company_provider_credentials" in statement_text:
                return _ScalarResult(credentials_row)
            if "FROM company_provider_mappings" in statement_text:
                return _ScalarResult(None)
            raise AssertionError("unexpected query")

    with pytest.raises(ListingPreconditionFailed) as excinfo:
        await sims._load_credentials(
            COMPANY_ID,
            "moabits",
            _Db(),
            Settings(fernet_key=fernet_key),
        )

    assert excinfo.value.extra.get("provider") == "moabits"


@pytest.mark.asyncio
async def test_load_moabits_credentials_uses_mapping_company_code() -> None:
    fernet_key = Fernet.generate_key().decode()
    credentials_row = CompanyProviderCredentials(
        company_id=COMPANY_ID,
        provider="moabits",
        credentials_enc=encrypt_credentials(
            {
                "base_url": "https://www.api.myorion.co",
                "x_api_key": "secret-key",
            },
            fernet_key,
        ),
        account_scope={},
        active=True,
        created_at=datetime.now(),
    )
    mapping = CompanyProviderMapping(
        company_id=COMPANY_ID,
        provider="moabits",
        provider_company_code="48123-99",
        provider_company_name="3 POINTECH S.A.S.",
        clie_id=2659,
        settings={},
        active=True,
        created_at=datetime.now(),
        updated_at=datetime.now(),
    )

    class _Db:
        async def execute(self, stmt):
            statement_text = str(stmt)
            if "FROM company_provider_credentials" in statement_text:
                return _ScalarResult(credentials_row)
            if "FROM company_provider_mappings" in statement_text:
                return _ScalarResult(mapping)
            raise AssertionError("unexpected query")

    loaded = await sims._load_credentials(
        COMPANY_ID,
        "moabits",
        _Db(),
        Settings(fernet_key=fernet_key),
    )

    assert loaded["company_code"] == "48123-99"
    assert loaded["provider_company_mapping"] == {
        "companyCode": "48123-99",
        "companyName": "3 POINTECH S.A.S.",
        "clie_id": 2659,
    }


def test_provider_listing_builds_canonical_filters(monkeypatch) -> None:
    captured = {}
    client = _client(AppRole.member)

    async def _list_provider(provider, cursor, limit, filters, *args):
        captured["provider"] = provider
        captured["limit"] = limit
        captured["filters"] = filters
        return SimListOut(items=[], next_cursor=None, total=None)

    monkeypatch.setattr(sims, "_list_via_provider_search", _list_provider)

    response = client.get(
        "/v1/sims?provider=kite&status=active&iccid=893&custom=customField1=acme"
    )

    assert response.status_code == 200
    assert captured["provider"] == "kite"
    assert captured["limit"] == 50
    assert captured["filters"].status.value == "active"
    assert captured["filters"].iccid == "893"
    assert captured["filters"].custom == {"customField1": "acme"}


def test_tele2_listing_requires_modified_since() -> None:
    client = _client(AppRole.member)

    response = client.get("/v1/sims?provider=tele2")

    assert response.status_code == 400
    assert response.json() == {
        "errorMessage": "ModifiedSince is required.",
        "errorCode": "10000003",
    }


def test_list_sims_provider_query_is_enum() -> None:
    client = _client(AppRole.member)

    response = client.get("/openapi.json")

    assert response.status_code == 200
    provider_schema = next(
        param["schema"]
        for path, methods in response.json()["paths"].items()
        if path == "/v1/sims"
        for param in methods["get"]["parameters"]
        if param["name"] == "provider"
    )
    assert provider_schema["anyOf"][0]["$ref"] == "#/components/schemas/Provider"
    assert response.json()["components"]["schemas"]["Provider"]["enum"] == [
        "kite",
        "tele2",
        "moabits",
    ]


def test_list_sims_documents_tele2_modified_since_rules() -> None:
    client = _client(AppRole.member)

    response = client.get("/openapi.json")

    assert response.status_code == 200
    params = response.json()["paths"]["/v1/sims"]["get"]["parameters"]
    modified_since = next(
        param for param in params if param["name"] == "modified_since"
    )
    modified_till = next(param for param in params if param["name"] == "modified_till")
    assert "Provider support: tele2" in modified_since["description"]
    assert "kite" in modified_since["description"]
    assert "moabits" in modified_since["description"]
    assert "modified_since + 1 year" in modified_till["description"]


def test_list_sims_rejects_unknown_provider() -> None:
    client = _client(AppRole.member)

    response = client.get("/v1/sims?provider=unknown")

    assert response.status_code == 422


def test_subscription_output_includes_normalized_blocks() -> None:
    out = sims._to_out(
        Subscription(
            iccid="8988216716970004975",
            msisdn="882351697004975",
            imsi="901161697004975",
            status=AdministrativeStatus.ACTIVE,
            native_status="ACTIVATED",
            provider="tele2",
            company_id=str(COMPANY_ID),
            activated_at=None,
            updated_at=datetime(2016, 7, 6, 22, 4, 4),
            provider_fields={
                "detail_enriched": True,
                "imei": "12345",
                "rate_plan": "hphlr rp1",
                "communication_plan": "CP_Basic_ON",
                "account_id": "100020620",
                "date_shipped": "2016-06-27 07:00:00.000+0000",
                "account_custom_1": "78",
            },
        )
    )

    assert out.detail_level == "detail"
    assert out.normalized["identity"]["imei"] == "12345"
    assert out.normalized["plan"]["name"] == "hphlr rp1"
    assert out.normalized["plan"]["communication_plan"] == "CP_Basic_ON"
    assert out.normalized["customer"]["account_id"] == "100020620"
    assert out.normalized["hardware"]["shipped_at"] == "2016-06-27T07:00:00Z"
    assert out.normalized["custom_fields"] == {"account_custom_1": "78"}


def test_moabits_summary_output_normalizes_minimal_simlist_fields() -> None:
    out = sims._to_out(
        Subscription(
            iccid="8910300000001880253",
            msisdn=None,
            imsi=None,
            status=AdministrativeStatus.SUSPENDED,
            native_status="Suspended",
            provider="moabits",
            company_id=str(COMPANY_ID),
            activated_at=None,
            updated_at=None,
            provider_fields={
                "detail_enriched": False,
                "data_service": "Disabled",
                "sms_service": "Enabled",
                "services": ["sms"],
            },
        )
    )

    assert out.detail_level == "summary"
    assert out.normalized["identity"]["iccid"] == "8910300000001880253"
    assert out.normalized["status"]["value"] == "suspended"
    assert out.normalized["status"]["native"] == "Suspended"
    assert out.normalized["services"]["active"] == ["sms"]
    assert out.normalized["services"]["data_service"] is False
    assert out.normalized["services"]["sms_service"] is True


@pytest.mark.asyncio
async def test_provider_listing_commits_routing_upserts_once(monkeypatch) -> None:
    class _Db:
        def __init__(self) -> None:
            self.execute_calls = 0
            self.commit_calls = 0

        async def execute(self, stmt):
            self.execute_calls += 1

        async def commit(self):
            self.commit_calls += 1

    class _SearchProvider:
        async def list_subscriptions(self, credentials, *, cursor, limit, filters):
            return (
                [
                    Subscription(
                        iccid="8934071100303041838",
                        msisdn=None,
                        imsi=None,
                        status=AdministrativeStatus.ACTIVE,
                        native_status="active",
                        provider="kite",
                        company_id=str(COMPANY_ID),
                        activated_at=None,
                        updated_at=None,
                    ),
                    Subscription(
                        iccid="8934071100303041796",
                        msisdn=None,
                        imsi=None,
                        status=AdministrativeStatus.ACTIVE,
                        native_status="active",
                        provider="kite",
                        company_id=str(COMPANY_ID),
                        activated_at=None,
                        updated_at=None,
                    ),
                ],
                None,
            )

    class _SearchRegistry:
        def get(self, provider):
            return _SearchProvider()

    async def _credentials(*args, **kwargs):
        return {}

    monkeypatch.setattr(sims, "_load_credentials", _credentials)
    db = _Db()

    await sims._list_via_provider_search(
        "kite",
        cursor=None,
        limit=50,
        filters=sims._build_filters(
            status_filter=None,
            modified_since=None,
            modified_till=None,
            iccid=None,
            imsi=None,
            msisdn=None,
            custom=None,
        ),
        company_id=COMPANY_ID,
        db=db,
        settings=Settings(),
        registry=_SearchRegistry(),
    )

    assert db.execute_calls == 2
    assert db.commit_calls == 1


@pytest.mark.asyncio
async def test_global_iccid_search_uses_routing_map_for_moabits(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str]] = []

    class _Db:
        async def commit(self):
            raise AssertionError("mapped ICCID lookup should not rewrite routing")

    class _Routing:
        provider = "moabits"

    class _Provider:
        async def get_subscription(self, iccid, credentials):
            calls.append(("get_subscription", iccid))
            return Subscription(
                iccid=iccid,
                msisdn=None,
                imsi=None,
                status=AdministrativeStatus.ACTIVE,
                native_status="Active",
                provider="moabits",
                company_id=str(COMPANY_ID),
                activated_at=None,
                updated_at=None,
            )

        async def list_subscriptions(self, credentials, *, cursor, limit, filters):
            raise AssertionError("Moabits listing filters should not be called")

    class _Registry:
        def get(self, provider):
            assert provider == "moabits"
            return _Provider()

    async def _find(*args, **kwargs):
        return _Routing()

    async def _credentials(*args, **kwargs):
        return {
            "base_url": "https://www.api.myorion.co",
            "x_api_key": "secret-key",
            "company_code": "48123",
        }

    monkeypatch.setattr(sims, "_find_routing", _find)
    monkeypatch.setattr(sims, "_load_credentials", _credentials)

    result = await sims._list_via_routing_index(
        cursor=None,
        limit=50,
        filters=sims._build_filters(
            status_filter=None,
            modified_since=None,
            modified_till=None,
            iccid="8910300000001880253",
            imsi=None,
            msisdn=None,
            custom=None,
        ),
        company_id=COMPANY_ID,
        db=_Db(),
        settings=Settings(),
        registry=_Registry(),
    )

    assert calls == [("get_subscription", "8910300000001880253")]
    assert result.partial is False
    assert result.items[0].provider == "moabits"
    assert [
        (status.provider, status.status, status.count)
        for status in result.provider_statuses
    ] == [
        ("kite", "not_queried", 0),
        ("tele2", "not_queried", 0),
        ("moabits", "ok", 1),
    ]


@pytest.mark.asyncio
async def test_global_iccid_search_queries_moabits_when_unmapped(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str | None]] = []

    class _Db:
        def __init__(self) -> None:
            self.commit_calls = 0

        async def commit(self):
            self.commit_calls += 1

    class _Provider:
        def __init__(self, provider):
            self.provider = provider

        async def list_subscriptions(self, credentials, *, cursor, limit, filters):
            calls.append((self.provider, filters.iccid))
            if self.provider == "moabits":
                return (
                    [
                        Subscription(
                            iccid=filters.iccid,
                            msisdn=None,
                            imsi=None,
                            status=AdministrativeStatus.ACTIVE,
                            native_status="Active",
                            provider="moabits",
                            company_id=str(COMPANY_ID),
                            activated_at=None,
                            updated_at=None,
                        )
                    ],
                    None,
                )
            if self.provider == "tele2":
                assert filters.modified_since is not None
            else:
                assert filters.modified_since is None
            return [], None

    class _Registry:
        def get(self, provider):
            assert provider in {"kite", "tele2", "moabits"}
            return _Provider(provider)

    async def _find(*args, **kwargs):
        return None

    async def _credentials(*args, **kwargs):
        return {}

    async def _upsert(*args, **kwargs):
        return None

    monkeypatch.setattr(sims, "_find_routing", _find)
    monkeypatch.setattr(sims, "_load_credentials", _credentials)
    monkeypatch.setattr(sims, "_upsert_routing", _upsert)
    db = _Db()

    result = await sims._list_via_routing_index(
        cursor=None,
        limit=50,
        filters=sims._build_filters(
            status_filter=None,
            modified_since=None,
            modified_till=None,
            iccid="8934070100000000001",
            imsi=None,
            msisdn=None,
            custom=None,
        ),
        company_id=COMPANY_ID,
        db=db,
        settings=Settings(),
        registry=_Registry(),
    )

    assert calls == [
        ("kite", "8934070100000000001"),
        ("tele2", "8934070100000000001"),
        ("moabits", "8934070100000000001"),
    ]
    assert db.commit_calls == 1
    assert result.partial is False
    assert [item.provider for item in result.items] == ["moabits"]
    assert result.failed_providers == []
    assert [
        (status.provider, status.status, status.count)
        for status in result.provider_statuses
    ] == [
        ("kite", "ok", 0),
        ("tele2", "ok", 0),
        ("moabits", "ok", 1),
    ]


# ── Lazy ICCID resolution (routing map → fan-out) ──────────────────────────────


@pytest.fixture(autouse=True)
def _clear_iccid_negative_cache():
    sims._iccid_negative_cache.clear()
    yield
    sims._iccid_negative_cache.clear()


def _make_subscription(iccid: str, provider: str) -> Subscription:
    return Subscription(
        iccid=iccid,
        msisdn=None,
        imsi=None,
        status=AdministrativeStatus.ACTIVE,
        native_status="active",
        provider=provider,
        company_id=str(COMPANY_ID),
        activated_at=None,
        updated_at=None,
    )


@pytest.mark.asyncio
async def test_resolve_or_discover_hits_routing_map_without_fanout(
    monkeypatch,
) -> None:
    async def _find(iccid, company_id, db):
        return _Routing()

    async def _discover(*args, **kwargs):
        raise AssertionError("routing-map hit must not trigger fan-out")

    monkeypatch.setattr(sims, "_find_routing", _find)
    monkeypatch.setattr(sims, "_discover_iccid_across_providers", _discover)

    routing, prefetched = await sims._resolve_routing_or_discover(
        "8934070100000000001",
        COMPANY_ID,
        db=object(),
        settings=Settings(),
        registry=object(),
    )

    assert routing.provider == "tele2"
    assert prefetched is None


@pytest.mark.asyncio
async def test_resolve_or_discover_falls_back_to_fanout_when_unmapped(
    monkeypatch,
) -> None:
    discovery_calls: list[str] = []
    discovered_sub = _make_subscription("8934070100000000001", "kite")

    state: dict[str, int] = {"find_calls": 0}

    async def _find(iccid, company_id, db):
        state["find_calls"] += 1
        if state["find_calls"] == 1:
            return None
        return _Routing()

    async def _discover(iccid, company_id, db, settings, registry):
        discovery_calls.append(iccid)
        return discovered_sub

    monkeypatch.setattr(sims, "_find_routing", _find)
    monkeypatch.setattr(sims, "_discover_iccid_across_providers", _discover)

    routing, prefetched = await sims._resolve_routing_or_discover(
        "8934070100000000001",
        COMPANY_ID,
        db=object(),
        settings=Settings(),
        registry=object(),
    )

    assert discovery_calls == ["8934070100000000001"]
    assert prefetched is discovered_sub
    assert routing.provider == "tele2"


@pytest.mark.asyncio
async def test_resolve_or_discover_raises_and_caches_negative_on_miss(
    monkeypatch,
) -> None:
    discovery_calls: list[str] = []

    async def _find(iccid, company_id, db):
        return None

    async def _discover(iccid, company_id, db, settings, registry):
        discovery_calls.append(iccid)
        return None

    monkeypatch.setattr(sims, "_find_routing", _find)
    monkeypatch.setattr(sims, "_discover_iccid_across_providers", _discover)

    from app.shared.errors import SubscriptionNotFound

    with pytest.raises(SubscriptionNotFound):
        await sims._resolve_routing_or_discover(
            "0000000000000000000",
            COMPANY_ID,
            db=object(),
            settings=Settings(),
            registry=object(),
        )

    with pytest.raises(SubscriptionNotFound):
        await sims._resolve_routing_or_discover(
            "0000000000000000000",
            COMPANY_ID,
            db=object(),
            settings=Settings(),
            registry=object(),
        )

    assert discovery_calls == ["0000000000000000000"]


@pytest.mark.asyncio
async def test_discover_iccid_skips_providers_without_iccid_filter(
    monkeypatch,
) -> None:
    list_calls: list[str] = []

    class _Adapter:
        def __init__(self, provider: str, supports: bool):
            self.provider = provider
            self._supports = supports

        def supports_list_filter(self, filter_name: str) -> bool:
            return self._supports and filter_name == "iccid"

        def bootstrap_filters(self):
            from app.subscriptions.domain import SubscriptionSearchFilters

            return SubscriptionSearchFilters()

        async def list_subscriptions(self, creds, *, cursor, limit, filters):
            list_calls.append(self.provider)
            if self.provider == "tele2":
                return [_make_subscription(filters.iccid, "tele2")], None
            return [], None

    class _Registry:
        def get(self, provider):
            return _Adapter(
                provider,
                supports=provider in {"kite", "tele2"},
            )

    class _Db:
        def __init__(self) -> None:
            self.commit_calls = 0

        async def execute(self, stmt):
            return None

        async def commit(self):
            self.commit_calls += 1

    async def _credentials(*args, **kwargs):
        return {}

    async def _upsert(*args, **kwargs):
        return None

    monkeypatch.setattr(sims, "_load_credentials", _credentials)
    monkeypatch.setattr(sims, "_upsert_routing", _upsert)
    db = _Db()

    result = await sims._discover_iccid_across_providers(
        "8934070100000000001",
        COMPANY_ID,
        db=db,
        settings=Settings(),
        registry=_Registry(),
    )

    assert result is not None
    assert result.provider == "tele2"
    assert "moabits" not in list_calls
    assert set(list_calls) == {"kite", "tele2"}
    assert db.commit_calls == 1


@pytest.mark.asyncio
async def test_discover_iccid_treats_provider_errors_as_provider_misses(
    monkeypatch,
) -> None:
    class _Adapter:
        def __init__(self, provider: str):
            self.provider = provider

        def supports_list_filter(self, filter_name: str) -> bool:
            return filter_name == "iccid"

        def bootstrap_filters(self):
            from app.subscriptions.domain import SubscriptionSearchFilters

            return SubscriptionSearchFilters()

        async def list_subscriptions(self, creds, *, cursor, limit, filters):
            if self.provider == "kite":
                raise RuntimeError("kite is down")
            if self.provider == "tele2":
                return [_make_subscription(filters.iccid, "tele2")], None
            return [], None

    class _Registry:
        def get(self, provider):
            return _Adapter(provider)

    class _Db:
        async def execute(self, stmt):
            return None

        async def commit(self):
            return None

    async def _credentials(*args, **kwargs):
        return {}

    async def _upsert(*args, **kwargs):
        return None

    monkeypatch.setattr(sims, "_load_credentials", _credentials)
    monkeypatch.setattr(sims, "_upsert_routing", _upsert)

    result = await sims._discover_iccid_across_providers(
        "8934070100000000001",
        COMPANY_ID,
        db=_Db(),
        settings=Settings(),
        registry=_Registry(),
    )

    assert result is not None
    assert result.provider == "tele2"


def test_set_status_does_not_trigger_fanout_on_routing_miss(monkeypatch) -> None:
    async def _find(iccid, company_id, db):
        return None

    async def _discover(*args, **kwargs):
        raise AssertionError("write endpoints must remain strict")

    monkeypatch.setattr(sims, "_find_routing", _find)
    monkeypatch.setattr(sims, "_discover_iccid_across_providers", _discover)

    client = _client(AppRole.admin)
    response = client.put(
        "/v1/sims/8934070100000000001/status",
        headers={"Idempotency-Key": "k-1"},
        json={"target": "active"},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "subscription.not_found"
