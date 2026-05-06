import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.database import get_db
from app.identity.dependencies import get_current_profile
from app.identity.models.profile import AppRole, Profile
from app.providers.registry import ProviderRegistry
from app.shared.errors import DomainError
from app.subscriptions.routers import sims
from app.subscriptions.schemas.sim import SimListOut

COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


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


def _client(role: AppRole, registry: ProviderRegistry | _Registry | None = None) -> TestClient:
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
    app.dependency_overrides[sims.get_registry] = lambda: registry or _Registry(_Provider())
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


def test_provider_listing_builds_canonical_filters(monkeypatch) -> None:
    captured = {}
    client = _client(AppRole.member)

    async def _list_provider(provider, cursor, limit, filters, *args):
        captured["provider"] = provider
        captured["filters"] = filters
        return SimListOut(items=[], next_cursor=None, total=None)

    monkeypatch.setattr(sims, "_list_via_provider_search", _list_provider)

    response = client.get(
        "/v1/sims?provider=kite&status=active&iccid=893&custom=customField1=acme"
    )

    assert response.status_code == 200
    assert captured["provider"] == "kite"
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
    modified_till = next(
        param for param in params if param["name"] == "modified_till"
    )
    assert "Required when provider=tele2" in modified_since["description"]
    assert "modified_since + 1 year" in modified_till["description"]


def test_list_sims_rejects_unknown_provider() -> None:
    client = _client(AppRole.member)

    response = client.get("/v1/sims?provider=unknown")

    assert response.status_code == 422
