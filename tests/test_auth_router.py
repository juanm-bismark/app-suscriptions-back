import uuid

import pytest

from app.config import Settings
from app.identity.models.profile import AppRole, Profile
from app.identity.models.refresh_token import RefreshToken
from app.identity.models.user import User
from app.identity.routers.auth import SignupRequest, signup
from app.tenancy.models.company import Company

COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
DUPLICATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
JWT_SECRET = "test-secret-with-at-least-thirty-two-bytes"


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _SignupDb:
    def __init__(
        self,
        *,
        duplicate_user_id: uuid.UUID | None = None,
        duplicate_company_id: uuid.UUID | None = None,
    ) -> None:
        self.duplicate_user_id = duplicate_user_id
        self.duplicate_company_id = duplicate_company_id
        self.added = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, statement):
        statement_text = str(statement)
        first_line = statement_text.splitlines()[0].strip()
        if "FROM users" in statement_text and first_line == "SELECT users.id":
            return _Result(self.duplicate_user_id)
        if "FROM companies" in statement_text and first_line == "SELECT companies.id":
            return _Result(self.duplicate_company_id)
        return _Result(None)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _admin_profile() -> Profile:
    return Profile(id=USER_ID, company_id=COMPANY_ID, role=AppRole.admin)


@pytest.mark.asyncio
async def test_signup_normalizes_email_and_company_name() -> None:
    db = _SignupDb()

    await signup(
        SignupRequest(
            email="  New@Example.COM  ",
            password="secret",
            company_name="  Acme  ",
        ),
        db,
        Settings(jwt_secret=JWT_SECRET),
        None,
    )

    user = next(row for row in db.added if isinstance(row, User))
    company = next(row for row in db.added if isinstance(row, Company))
    assert user.email == "new@example.com"
    assert company.name == "Acme"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_signup_rejects_duplicate_email_before_insert() -> None:
    db = _SignupDb(duplicate_user_id=DUPLICATE_ID)

    with pytest.raises(Exception) as excinfo:
        await signup(
            SignupRequest(
                email="taken@example.com",
                password="secret",
                company_name="Acme",
            ),
            db,
            Settings(jwt_secret=JWT_SECRET),
            None,
        )

    assert getattr(excinfo.value, "status_code", None) == 409
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_signup_rejects_duplicate_company_name_before_insert() -> None:
    db = _SignupDb(duplicate_company_id=DUPLICATE_ID)

    with pytest.raises(Exception) as excinfo:
        await signup(
            SignupRequest(
                email="new@example.com",
                password="secret",
                company_name="Existing",
            ),
            db,
            Settings(jwt_secret=JWT_SECRET),
            None,
        )

    assert getattr(excinfo.value, "status_code", None) == 409
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_authenticated_signup_normalizes_invited_user_email() -> None:
    db = _SignupDb()

    await signup(
        SignupRequest(
            email="  Invited@Example.COM  ",
            password="secret",
            role="member",
        ),
        db,
        Settings(jwt_secret=JWT_SECRET),
        _admin_profile(),
    )

    user = next(row for row in db.added if isinstance(row, User))
    assert user.email == "invited@example.com"
    assert not any(isinstance(row, Company) for row in db.added)
    assert any(isinstance(row, RefreshToken) for row in db.added)
    assert db.commits == 1
