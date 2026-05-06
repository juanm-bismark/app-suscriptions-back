from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.providers.routers import router


def _client(settings: Settings | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/v1")
    if settings is not None:
        app.dependency_overrides[get_settings] = lambda: settings
    return TestClient(app)


def test_provider_capabilities_exposes_minimum_contract() -> None:
    client = _client(Settings(lifecycle_writes_enabled=True))

    response = client.get("/v1/providers/kite/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "kite"
    assert set(body["capabilities"]) == {
        "list_subscriptions",
        "get_subscription",
        "get_usage",
        "get_presence",
        "set_administrative_status",
        "purge",
        "status_history",
        "aggregated_usage",
        "plan_catalog",
        "quota_management",
    }
    assert body["capabilities"]["purge"]["status"] == "supported"
    assert body["capabilities"]["status_history"]["status"] == "supported"


def test_moabits_purge_capability_supported_when_writes_enabled() -> None:
    client = _client(Settings(lifecycle_writes_enabled=True))

    response = client.get("/v1/providers/moabits/capabilities")

    assert response.status_code == 200
    purge = response.json()["capabilities"]["purge"]
    assert purge["status"] == "supported"
    assert "/api/sim/purge/" in purge["reason"]


def test_moabits_purge_capability_requires_feature_flag_when_writes_disabled() -> None:
    client = _client(Settings(lifecycle_writes_enabled=False))

    response = client.get("/v1/providers/moabits/capabilities")

    assert response.status_code == 200
    assert response.json()["capabilities"]["purge"]["status"] == "requires_feature_flag"
