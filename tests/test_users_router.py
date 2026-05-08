import uuid

from app.identity.models.profile import AppRole, Profile
from app.identity.routers.users import _list_users_query

COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


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
