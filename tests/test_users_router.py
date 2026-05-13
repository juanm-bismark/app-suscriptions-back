import uuid

import pytest

from app.identity.auth_utils import hash_password, verify_password
from app.identity.models.profile import AppRole, Profile
from app.identity.models.user import User
from app.identity.routers.users import (
    _attach_profile_emails,
    _list_users_query,
    create_user,
    update_user,
)
from app.identity.schemas.user import UserCreate, UserUpdate

COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
TARGET_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")
DUPLICATE_ID = uuid.UUID("00000000-0000-0000-0000-000000000004")


def _profile(role: AppRole) -> Profile:
    return Profile(id=USER_ID, company_id=COMPANY_ID, role=role)


def test_manager_listing_excludes_admins() -> None:
    query = _list_users_query(_profile(AppRole.manager))

    query_text = str(query)
    assert "profiles.company_id" in query_text
    assert "profiles.role IN" in query_text


def test_admin_listing_keeps_full_company_scope() -> None:
    query = _list_users_query(_profile(AppRole.admin))

    query_text = str(query)
    assert "profiles.company_id" in query_text
    assert "profiles.role IN" not in query_text


def test_listing_filters_by_name_or_email_when_q_is_present() -> None:
    query = _list_users_query(_profile(AppRole.admin), "ss")

    query_text = str(query)
    assert "JOIN users" in query_text
    assert "lower(profiles.full_name)" in query_text
    assert "lower(users.email)" in query_text


def test_listing_ignores_blank_q() -> None:
    query = _list_users_query(_profile(AppRole.admin), "   ")

    query_text = str(query)
    assert "JOIN users" not in query_text


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _UserUpdateDb:
    def __init__(
        self,
        profile: Profile,
        user: User,
        duplicate_user_id: uuid.UUID | None = None,
    ) -> None:
        self.profile = profile
        self.user = user
        self.duplicate_user_id = duplicate_user_id
        self.commits = 0
        self.rollbacks = 0
        self.refreshes = 0

    async def execute(self, statement):
        statement_text = str(statement)
        if "FROM profiles" in statement_text:
            return _Result(self.profile)
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


class _UserCreateDb:
    def __init__(self, duplicate_user_id: uuid.UUID | None = None) -> None:
        self.duplicate_user_id = duplicate_user_id
        self.added = []
        self.commits = 0
        self.rollbacks = 0
        self.refreshes = 0

    async def execute(self, statement):
        statement_text = str(statement)
        if (
            "FROM users" in statement_text
            and statement_text.splitlines()[0].strip() == "SELECT users.id"
        ):
            return _Result(self.duplicate_user_id)
        return _Result(None)

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def refresh(self, row):
        self.refreshes += 1


def _target_profile() -> Profile:
    return Profile(id=TARGET_ID, company_id=COMPANY_ID, role=AppRole.member)


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _EmailRow:
    def __init__(self, id, email):
        self.id = id
        self.email = email


@pytest.mark.asyncio
async def test_attach_profile_emails_adds_email_for_user_table() -> None:
    profiles = [_target_profile()]

    class _Db:
        async def execute(self, statement):
            return _RowsResult([_EmailRow(TARGET_ID, "target@example.com")])

    await _attach_profile_emails(profiles, _Db())

    assert profiles[0].email == "target@example.com"


@pytest.mark.asyncio
async def test_create_user_rejects_public_role() -> None:
    db = _UserCreateDb()

    with pytest.raises(Exception) as excinfo:
        await create_user(
            UserCreate(email="new@example.com", password="secret", role=AppRole.member),
            _profile(AppRole.public),
            db,
        )

    assert getattr(excinfo.value, "status_code", None) == 403
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_create_user_rejects_duplicate_email_before_insert() -> None:
    db = _UserCreateDb(duplicate_user_id=DUPLICATE_ID)

    with pytest.raises(Exception) as excinfo:
        await create_user(
            UserCreate(email="taken@example.com", password="secret", role=AppRole.member),
            _profile(AppRole.admin),
            db,
        )

    assert getattr(excinfo.value, "status_code", None) == 409
    assert db.added == []
    assert db.commits == 0


@pytest.mark.asyncio
async def test_create_user_normalizes_email_after_availability_check() -> None:
    db = _UserCreateDb()

    result = await create_user(
        UserCreate(email="  New@Example.COM  ", password="secret", role=AppRole.member),
        _profile(AppRole.admin),
        db,
    )

    created_user = next(row for row in db.added if isinstance(row, User))
    assert created_user.email == "new@example.com"
    assert result.email == "new@example.com"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_patch_user_ignores_blank_password() -> None:
    old_hash = hash_password("old-secret")
    target = _target_profile()
    user = User(id=TARGET_ID, email="old@example.com", hashed_password=old_hash)
    db = _UserUpdateDb(target, user)

    result = await update_user(
        TARGET_ID,
        UserUpdate(full_name="Renamed", password=""),
        _profile(AppRole.admin),
        db,
    )

    assert result.full_name == "Renamed"
    assert result.email == "old@example.com"
    assert user.hashed_password == old_hash
    assert verify_password("old-secret", user.hashed_password)
    assert db.commits == 1


@pytest.mark.asyncio
async def test_patch_user_updates_password_when_non_blank() -> None:
    old_hash = hash_password("old-secret")
    target = _target_profile()
    user = User(id=TARGET_ID, email="old@example.com", hashed_password=old_hash)
    db = _UserUpdateDb(target, user)

    await update_user(
        TARGET_ID,
        UserUpdate(password="new-secret"),
        _profile(AppRole.admin),
        db,
    )

    assert user.hashed_password != old_hash
    assert verify_password("new-secret", user.hashed_password)
    assert db.commits == 1


@pytest.mark.asyncio
async def test_patch_user_rejects_duplicate_email_before_overwriting() -> None:
    target = _target_profile()
    user = User(
        id=TARGET_ID,
        email="old@example.com",
        hashed_password=hash_password("old-secret"),
    )
    db = _UserUpdateDb(target, user, duplicate_user_id=DUPLICATE_ID)

    with pytest.raises(Exception) as excinfo:
        await update_user(
            TARGET_ID,
            UserUpdate(email="taken@example.com", full_name="Should Not Apply"),
            _profile(AppRole.admin),
            db,
        )

    assert getattr(excinfo.value, "status_code", None) == 409
    assert target.full_name is None
    assert user.email == "old@example.com"
    assert db.commits == 0


@pytest.mark.asyncio
async def test_patch_user_allows_keeping_current_email_case_insensitively() -> None:
    target = _target_profile()
    user = User(
        id=TARGET_ID,
        email="Old@Example.COM",
        hashed_password=hash_password("old-secret"),
    )
    db = _UserUpdateDb(target, user, duplicate_user_id=DUPLICATE_ID)

    result = await update_user(
        TARGET_ID,
        UserUpdate(email="old@example.com"),
        _profile(AppRole.admin),
        db,
    )

    assert result.email == "old@example.com"
    assert user.email == "old@example.com"
    assert db.commits == 1


@pytest.mark.asyncio
async def test_patch_user_rejects_blank_email_without_overwriting() -> None:
    target = _target_profile()
    user = User(
        id=TARGET_ID,
        email="old@example.com",
        hashed_password=hash_password("old-secret"),
    )
    db = _UserUpdateDb(target, user)

    with pytest.raises(Exception) as excinfo:
        await update_user(
            TARGET_ID,
            UserUpdate(email="  "),
            _profile(AppRole.admin),
            db,
        )

    assert getattr(excinfo.value, "status_code", None) == 422
    assert user.email == "old@example.com"
    assert db.commits == 0
