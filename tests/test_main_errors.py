from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import domain_error_handler
from app.shared.errors import ProviderUnavailable


def test_domain_error_handler_sets_retry_after_header() -> None:
    app = FastAPI()
    app.add_exception_handler(ProviderUnavailable, domain_error_handler)

    @app.get("/boom")
    async def boom() -> None:
        raise ProviderUnavailable(
            detail="circuit breaker is OPEN for provider 'kite'",
            retry_after=30,
        )

    response = TestClient(app).get("/boom")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "30"
    assert response.json()["retry_after"] == "30"
