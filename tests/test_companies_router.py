import uuid
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import get_db
from app.identity.dependencies import get_current_profile
from app.identity.models.profile import AppRole, Profile
from app.tenancy.models.company import Company
from app.tenancy.routers import companies

COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


class _ScalarResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Db:
    def __init__(self) -> None:
        self.company = Company(
            id=COMPANY_ID,
            name="Bismark",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.committed = False

    async def execute(self, _statement):
        return _ScalarResult(self.company)

    async def commit(self):
        self.committed = True

    async def refresh(self, _row):
        return None


async def _db_override():
    yield _Db()


def _profile(role: AppRole) -> Profile:
    return Profile(id=USER_ID, company_id=COMPANY_ID, role=role)


def _client(role: AppRole) -> TestClient:
    app = FastAPI()
    app.include_router(companies.router, prefix="/v1")
    app.dependency_overrides[get_current_profile] = lambda: _profile(role)
    app.dependency_overrides[get_db] = _db_override
    return TestClient(app)


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
