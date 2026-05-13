import uuid

import pytest

from app.identity.auth_utils import hash_password, verify_password
from app.identity.models.profile import AppRole, Profile
from app.identity.models.user import User
from app.identity.routers.me import update_me
from app.identity.schemas.profile import ProfileUpdate

COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
DUPLICATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _MeUpdateDb:
    def __init__(self, user: User, duplicate_user_id: uuid.UUID | None = None) -> None:
        self.user = user
        self.duplicate_user_id = duplicate_user_id
        self.commits = 0
        self.rollbacks = 0
        self.refreshes = 0

    async def execute(self, statement):
        statement_text = str(statement)
        if "FROM users" in statement_text:
            if statement_text.splitlines()[0].strip() == "SELECT users.id":
                return _Result(self.duplicate_user_id)
            if statement_text.startswith("SELECT users.email"):
                return _Result(self.user.email)
            return _Result(self.user)
        return _Result(None)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, row):
        self.refreshes += 1


def _profile() -> Profile:
    return Profile(
        id=USER_ID,
        company_id=COMPANY_ID,
        role=AppRole.member,
        full_name="Old Name",
    )


@pytest.mark.asyncio
async def test_update_me_ignores_blank_password() -> None:
    old_hash = hash_password("old-secret")
    user = User(id=USER_ID, email="old@example.com", hashed_password=old_hash)
    profile = _profile()
    db = _MeUpdateDb(user)

    result = await update_me(
        ProfileUpdate(full_name="New Name", password=""),
        profile,
        db,
    )

    assert result.full_name == "New Name"
    assert result.email == "old@example.com"
    assert user.hashed_password == old_hash
    assert verify_password("old-secret", user.hashed_password)
    assert db.commits == 1


@pytest.mark.asyncio
async def test_update_me_updates_password_when_non_blank() -> None:
    old_hash = hash_password("old-secret")
    user = User(id=USER_ID, email="old@example.com", hashed_password=old_hash)
    db = _MeUpdateDb(user)

    await update_me(ProfileUpdate(password="new-secret"), _profile(), db)

    assert user.hashed_password != old_hash
    assert verify_password("new-secret", user.hashed_password)
    assert db.commits == 1


@pytest.mark.asyncio
async def test_update_me_rejects_duplicate_email_before_overwriting() -> None:
    user = User(
        id=USER_ID,
        email="old@example.com",
        hashed_password=hash_password("old-secret"),
    )
    db = _MeUpdateDb(user, duplicate_user_id=DUPLICATE_ID)
    profile = _profile()

    with pytest.raises(Exception) as excinfo:
        await update_me(
            ProfileUpdate(email="taken@example.com", full_name="New Name"),
            profile,
            db,
        )

    assert getattr(excinfo.value, "status_code", None) == 409
    assert profile.full_name == "Old Name"
    assert user.email == "old@example.com"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_update_me_allows_keeping_current_email_case_insensitively() -> None:
    user = User(
        id=USER_ID,
        email="Old@Example.COM",
        hashed_password=hash_password("old-secret"),
    )
    db = _MeUpdateDb(user, duplicate_user_id=DUPLICATE_ID)

    result = await update_me(ProfileUpdate(email="old@example.com"), _profile(), db)

    assert result.email == "old@example.com"
    assert user.email == "old@example.com"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_update_me_rejects_blank_email_without_overwriting() -> None:
    user = User(
        id=USER_ID,
        email="old@example.com",
        hashed_password=hash_password("old-secret"),
    )
    db = _MeUpdateDb(user)

    with pytest.raises(Exception) as excinfo:
        await update_me(ProfileUpdate(email="  "), _profile(), db)

    assert getattr(excinfo.value, "status_code", None) == 422
    assert user.email == "old@example.com"
    assert db.commits == 0
