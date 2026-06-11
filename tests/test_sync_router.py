"""Tests for sync / jobs endpoints (ADR-012 Fase B).

Uses direct handler calls with mock DB and Arq pool objects,
following the pattern in tests/test_users_router.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.identity.models.profile import AppRole, Profile
from app.providers.base import Provider
from app.shared.errors import JobNotFound, SyncAlreadyRunning
from app.sync.models import (
    KIND_ROUTING_SYNC,
    STATUS_DONE,
    STATUS_PENDING,
    STATUS_RUNNING,
    SyncJob,
)
from app.sync.router import get_job, get_sync_status, trigger_sync
from app.sync.schemas import SyncTriggerOut

COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
OTHER_COMPANY_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000010")
_NOW = datetime(2026, 5, 25, 2, 0, tzinfo=UTC)


def _profile(role: AppRole, company_id: uuid.UUID = COMPANY_ID) -> Profile:
    return Profile(id=USER_ID, company_id=company_id, role=role)


def _job(
    job_id: str = "testjob01",
    status: str = STATUS_DONE,
    provider: str = Provider.KITE,
    company_id: uuid.UUID = COMPANY_ID,
) -> SyncJob:
    return SyncJob(
        id=job_id,
        kind=KIND_ROUTING_SYNC,
        provider=provider,
        company_id=company_id,
        triggered_by=USER_ID,
        status=status,
        created_at=_NOW,
        started_at=_NOW,
        finished_at=_NOW if status == STATUS_DONE else None,
        progress_done=0,
        progress_total=None,
        cursor=None,
        result_url=None,
        result_expires_at=None,
        errors_json=[],
        params_json={"provider": provider},
    )


class _SingleResult:
    """Wraps a single value; supports scalar_one() and scalar_one_or_none()."""

    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


class _ListResult:
    """Wraps a list; supports scalars().all()."""

    def __init__(self, rows: list):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Db:
    """Call-ordered async DB mock.

    Pass `responses` as a list — each `execute()` call pops the next item.
    Each item may be a scalar value (returned via _SingleResult) or a list
    (returned via _ListResult).
    """

    def __init__(self, *responses) -> None:
        self._queue: list[Any] = list(responses)
        self.added: list[Any] = []
        self.commits = 0

    async def execute(self, statement, *args, **kwargs):
        if not self._queue:
            return _SingleResult(None)
        value = self._queue.pop(0)
        if isinstance(value, list):
            return _ListResult(value)
        return _SingleResult(value)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


class _FakeArqPool:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def enqueue_job(self, name: str, *args, _job_id: str | None = None, **kw) -> Any:
        self.calls.append({"name": name, "args": args, "_job_id": _job_id, "kw": kw})
        return _job_id


# ── trigger_sync ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_can_trigger_sync_happy_path() -> None:
    # First execute: dedup check returns None (no inflight job)
    db = _Db(None)
    pool = _FakeArqPool()

    result = await trigger_sync(
        provider=Provider.KITE,
        current=_profile(AppRole.admin),
        db=db,
        pool=pool,
    )

    assert isinstance(result, SyncTriggerOut)
    assert result.status_url == f"/v1/jobs/{result.job_id}"
    assert len(db.added) == 1
    assert isinstance(db.added[0], SyncJob)
    assert db.commits == 1
    assert len(pool.calls) == 1
    assert pool.calls[0]["name"] == "routing_sync_for_provider"
    assert pool.calls[0]["_job_id"] == result.job_id


@pytest.mark.asyncio
async def test_member_cannot_trigger_sync() -> None:
    """require_roles(admin) raises 403 before the handler body runs."""
    from fastapi import HTTPException

    from app.identity.dependencies import require_roles

    checker = require_roles(AppRole.admin)
    with pytest.raises(HTTPException) as exc_info:
        await checker(_profile(AppRole.member))

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_trigger_invalid_provider_returns_422() -> None:
    """Provider enum itself rejects bad values (FastAPI validates at HTTP layer)."""
    with pytest.raises(ValueError):
        Provider("bad_provider")


@pytest.mark.asyncio
async def test_trigger_when_inflight_returns_409_already_running() -> None:
    inflight = _job(job_id="inflight01", status=STATUS_RUNNING)
    # First execute() is the stale-job self-heal UPDATE (returns nothing useful);
    # second is the dedup SELECT returning the existing inflight job's ID.
    db = _Db(None, inflight.id)
    pool = _FakeArqPool()

    with pytest.raises(SyncAlreadyRunning) as exc_info:
        await trigger_sync(
            provider=Provider.KITE,
            current=_profile(AppRole.admin),
            db=db,
            pool=pool,
        )

    assert exc_info.value.code == "sync.already_running"
    assert db.added == []
    assert db.commits == 0
    assert pool.calls == []


@pytest.mark.asyncio
async def test_trigger_when_arq_unavailable_returns_503() -> None:
    """get_arq_pool raises 503 when app.state.arq_pool is None."""
    from fastapi import HTTPException

    from app.sync.queue import get_arq_pool

    class _FakeRequest:
        class app:
            class state:
                arq_pool = None

    with pytest.raises(HTTPException) as exc_info:
        await get_arq_pool(_FakeRequest())  # type: ignore[arg-type]

    assert exc_info.value.status_code == 503


# ── get_sync_status ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_returns_freshness_and_inflight_scoped_to_tenant() -> None:
    kite_done = _job("job-kite", STATUS_DONE, Provider.KITE, COMPANY_ID)
    tele2_done = _job("job-tele2", STATUS_DONE, Provider.TELE2, COMPANY_ID)
    running = _job("job-moabits", STATUS_RUNNING, Provider.MOABITS, COMPANY_ID)

    # First execute: done jobs query; second execute: inflight query
    db = _Db([kite_done, tele2_done], [running])

    result = await get_sync_status(current=_profile(AppRole.admin), db=db)

    assert len(result.freshness) == len(list(Provider))
    kite_fresh = next(f for f in result.freshness if f.provider == Provider.KITE)
    assert kite_fresh.last_finished_at == _NOW
    assert len(result.in_flight) == 1
    assert result.in_flight[0].job_id == "job-moabits"


@pytest.mark.asyncio
async def test_status_empty_when_no_jobs() -> None:
    db = _Db([], [])

    result = await get_sync_status(current=_profile(AppRole.member), db=db)

    assert all(f.last_finished_at is None for f in result.freshness)
    assert result.in_flight == []


# ── get_job ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_returns_row_for_same_tenant() -> None:
    job = _job("myjob01", STATUS_DONE, Provider.KITE, COMPANY_ID)
    db = _Db(job)

    result = await get_job(job_id="myjob01", current=_profile(AppRole.member), db=db)

    assert result.job_id == "myjob01"
    assert result.status == STATUS_DONE
    assert result.provider == Provider.KITE


@pytest.mark.asyncio
async def test_get_job_returns_404_for_other_tenant_non_admin() -> None:
    job = _job("otherjob", STATUS_DONE, Provider.KITE, OTHER_COMPANY_ID)
    db = _Db(job)

    with pytest.raises(JobNotFound) as exc_info:
        await get_job(
            job_id="otherjob",
            current=_profile(AppRole.member, COMPANY_ID),
            db=db,
        )

    assert exc_info.value.code == "job.not_found"


@pytest.mark.asyncio
async def test_get_job_admin_can_see_other_tenant_job() -> None:
    job = _job("otherjob", STATUS_DONE, Provider.KITE, OTHER_COMPANY_ID)
    db = _Db(job)

    result = await get_job(
        job_id="otherjob",
        current=_profile(AppRole.admin, COMPANY_ID),
        db=db,
    )

    assert result.job_id == "otherjob"


@pytest.mark.asyncio
async def test_get_job_returns_404_when_missing() -> None:
    db = _Db(None)

    with pytest.raises(JobNotFound) as exc_info:
        await get_job(job_id="ghost", current=_profile(AppRole.admin), db=db)

    assert exc_info.value.code == "job.not_found"


# ── routing_sync_for_provider task (Fase B-2) ─────────────────────────────────


@pytest.mark.asyncio
async def test_routing_sync_task_happy_path() -> None:
    """Task fetches creds, pages adapter, upserts rows, marks job done."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.subscriptions.domain import Subscription
    from app.sync.tasks import routing_sync_for_provider

    job_id = "taskjob01"
    provider = "kite"

    # ── Fake SyncJob row (for the initial select) ──────────────────────────────
    fake_job = _job(job_id, STATUS_PENDING, provider, COMPANY_ID)
    fake_job.cursor = None

    # ── Fake credentials row ───────────────────────────────────────────────────
    from app.tenancy.models.credentials import CompanyProviderCredentials

    fake_cred = CompanyProviderCredentials(
        id=uuid.uuid4(),
        company_id=COMPANY_ID,
        provider=provider,
        credentials_enc="ENCRYPTED",
        active=True,
    )

    # ── Fake adapter: one page of 2 subs, then done ───────────────────────────
    sub1 = Subscription(
        iccid="8931000000000000001", msisdn=None, imsi=None, status="ACTIVE",
        provider=provider, company_id=str(COMPANY_ID), activated_at=None, updated_at=None,
    )
    sub2 = Subscription(
        iccid="8931000000000000002", msisdn=None, imsi=None, status="ACTIVE",
        provider=provider, company_id=str(COMPANY_ID), activated_at=None, updated_at=None,
    )

    fake_adapter = MagicMock()
    fake_adapter.list_subscriptions = AsyncMock(return_value=([sub1, sub2], None))

    fake_registry = MagicMock()
    fake_registry.get.return_value = fake_adapter

    ctx = {"registry": fake_registry}

    # ── Fake session tracking calls ────────────────────────────────────────────
    executions: list[str] = []

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def execute(self, statement, *args, **kwargs):
            text = str(statement)
            executions.append(text[:60])
            if "FROM sync_jobs" in text:
                return _SingleResult(fake_job)
            if "FROM company_provider_credentials" in text:
                return _SingleResult(fake_cred)
            return _SingleResult(None)

        async def commit(self):
            pass

    def _fake_factory():
        return _FakeSession()

    with (
        patch("app.database._session_factory", _fake_factory),
        patch(
            "app.subscriptions.services.credentials.decrypt_credentials",
            return_value={"api_key": "k"},
        ),
        patch("app.config.get_settings") as mock_settings,
    ):
        mock_settings.return_value = MagicMock(fernet_key="fake-key")

        result = await routing_sync_for_provider(ctx, job_id, provider, str(COMPANY_ID))

    assert result["ok"] is True
    assert result["provider"] == provider
    assert result["total_done"] == 2
    fake_adapter.list_subscriptions.assert_awaited_once()


# ── worker settings ────────────────────────────────────────────────────────────


def test_worker_settings_includes_routing_sync_task() -> None:
    """WorkerSettings.functions must include routing_sync_for_provider."""
    from app.sync.tasks import routing_sync_for_provider

    try:
        from app.sync.worker import WorkerSettings

        assert routing_sync_for_provider in WorkerSettings.functions
    except RuntimeError:
        # REDIS_URL not set in this environment — verify function list directly
        # by inspecting the module source without instantiating WorkerSettings.
        import ast
        import pathlib

        src = pathlib.Path(__file__).parent.parent / "app" / "sync" / "worker.py"
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "WorkerSettings":
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name) and target.id == "functions":
                                src_repr = ast.unparse(item.value)
                                assert "routing_sync_for_provider" in src_repr
                                return
        pytest.fail("WorkerSettings.functions not found in worker.py")
