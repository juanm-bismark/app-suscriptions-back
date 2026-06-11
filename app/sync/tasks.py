"""Arq task definitions for async sync jobs (ADR-012)."""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

logger = logging.getLogger(__name__)

# Batch size passed to each adapter. Adapters clamp to their own max
# (Kite: 1000, Tele2: 50, Moabits: 500).
_SYNC_BATCH_SIZE = 500

# A job stuck pending/running past this age is treated as abandoned (the worker
# job_timeout ceiling is 1h, so 2h means it can never legitimately still run).
_STALE_JOB_CUTOFF = timedelta(hours=2)


async def _persist_job_failure(job_id: str, exc: Exception) -> None:
    from app.database import _session_factory
    from app.sync.models import STATUS_FAILED

    if _session_factory is None:
        return
    ts = datetime.now(UTC).isoformat()
    error_entry = json.dumps({"ts": ts, "kind": "task_error", "message": str(exc)})
    try:
        async with _session_factory() as db:
            await db.execute(
                text(
                    "UPDATE sync_jobs SET status = :status, finished_at = now(), "
                    "errors_json = errors_json || :entry::jsonb "
                    "WHERE id = :job_id"
                ),
                {"status": STATUS_FAILED, "entry": f"[{error_entry}]", "job_id": job_id},
            )
            await db.commit()
    except Exception:
        logger.exception("failed to persist error state for job %s", job_id)


async def routing_sync_for_provider(
    ctx: dict[str, Any],
    job_id: str,
    provider: str,
    company_id: str,
) -> dict[str, Any]:
    """Crawl all SIMs for a provider/company and upsert them into sim_routing_map.

    Called by the Arq worker. `ctx` contains 'registry' (ProviderRegistry)
    set up in on_startup.

    Flow:
      1. Mark sync_jobs row as running.
      2. Decrypt credentials from DB.
      3. Page through adapter.list_subscriptions() in a loop.
      4. Batch-upsert each page into sim_routing_map.
      5. Persist progress_done + cursor after each batch (resumability).
      6. Mark done / failed.
    """
    from app.config import get_settings
    from app.database import _session_factory
    from app.providers.base import SearchableProvider
    from app.providers.registry import ProviderRegistry
    from app.subscriptions.models.routing import SimRoutingMap
    from app.subscriptions.services.credentials import _load_credentials
    from app.sync.models import STATUS_DONE, STATUS_RUNNING, SyncJob

    if _session_factory is None:
        raise RuntimeError("Database engine not initialized in worker")

    settings = get_settings()
    registry: ProviderRegistry = ctx["registry"]
    adapter = registry.get(provider)
    if not callable(getattr(adapter, "list_subscriptions", None)):
        raise RuntimeError(f"Provider '{provider}' does not support subscription sync")
    searchable_adapter = cast(SearchableProvider, adapter)
    company_uuid = uuid.UUID(company_id)

    total_done = 0
    try:
        # ── Phase 1: mark running + fetch credentials ──────────────────────────
        async with _session_factory() as db:
            result = await db.execute(select(SyncJob).where(SyncJob.id == job_id))
            job = result.scalar_one()
            cursor: str | None = job.cursor  # resumability: pick up where we left off

            await db.execute(
                update(SyncJob)
                .where(SyncJob.id == job_id)
                .values(status=STATUS_RUNNING, started_at=func.now())
            )

            if not settings.fernet_key:
                raise RuntimeError("FERNET_KEY not configured")

            # _load_credentials injects the provider-specific scope the adapters
            # need — notably Moabits' `company_code` from the active provider
            # mapping, without which list_subscriptions returns zero SIMs.
            credentials = await _load_credentials(
                company_uuid, provider, db, settings
            )
            await db.commit()

        # ── Tele2: initialize date-range cursor on fresh start ─────────────────
        # Tele2 requires modifiedSince (max 365 days ago). On first run we cover
        # the last year; subsequent runs resume from the saved cursor.
        if provider == "tele2" and cursor is None:
            now = datetime.now(UTC).replace(microsecond=0)
            since = (now - timedelta(days=365)).replace(microsecond=0)
            cursor = (
                f"page:1"
                f"|since:{since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
                f"|till:{now.strftime('%Y-%m-%dT%H:%M:%SZ')}"
            )

        # ── Phase 2: paginate + upsert ─────────────────────────────────────────
        while True:
            subs, next_cursor = await searchable_adapter.list_subscriptions(
                credentials,
                cursor=cursor,
                limit=_SYNC_BATCH_SIZE,
            )

            if subs:
                rows = [
                    {
                        "iccid": s.iccid,
                        "provider": provider,
                        "company_id": company_uuid,
                        "last_seen_at": datetime.now(UTC),
                    }
                    for s in subs
                    if s.iccid
                ]
                if rows:
                    ins = pg_insert(SimRoutingMap).values(rows)
                    upsert = ins.on_conflict_do_update(
                        index_elements=["iccid"],
                        set_={
                            "provider": ins.excluded.provider,
                            "company_id": ins.excluded.company_id,
                            "last_seen_at": ins.excluded.last_seen_at,
                        },
                    )
                    async with _session_factory() as db:
                        await db.execute(upsert)
                        total_done += len(rows)
                        await db.execute(
                            update(SyncJob)
                            .where(SyncJob.id == job_id)
                            .values(progress_done=total_done, cursor=next_cursor)
                        )
                        await db.commit()

                    logger.info(
                        "sync_batch_done",
                        extra={
                            "job_id": job_id,
                            "provider": provider,
                            "batch": len(rows),
                            "total_done": total_done,
                        },
                    )

            if next_cursor is None:
                break
            cursor = next_cursor

        # ── Mark done ──────────────────────────────────────────────────────────
        async with _session_factory() as db:
            await db.execute(
                update(SyncJob)
                .where(SyncJob.id == job_id)
                .values(
                    status=STATUS_DONE,
                    finished_at=func.now(),
                    progress_done=total_done,
                    cursor=None,
                )
            )
            await db.commit()

        logger.info(
            "routing_sync_done",
            extra={"job_id": job_id, "provider": provider, "total_done": total_done},
        )

    except Exception as exc:
        await _persist_job_failure(job_id, exc)
        raise

    return {"ok": True, "job_id": job_id, "provider": provider, "total_done": total_done}


async def schedule_nightly_routing_sync(ctx: dict[str, Any]) -> dict[str, Any]:
    """Create routing sync jobs for every active company/provider credential."""
    from app.database import _session_factory
    from app.sync.models import (
        KIND_ROUTING_SYNC,
        STATUS_FAILED,
        STATUS_PENDING,
        STATUS_RUNNING,
        SyncJob,
    )
    from app.tenancy.models.credentials import CompanyProviderCredentials

    if _session_factory is None:
        raise RuntimeError("Database engine not initialized in worker")

    created = 0
    skipped = 0
    queued = 0
    async with _session_factory() as db:
        # Self-heal abandoned jobs (worker died, or an enqueue never landed) so
        # they don't block scheduling forever via the in-flight guard below.
        await db.execute(
            update(SyncJob)
            .where(
                and_(
                    SyncJob.kind == KIND_ROUTING_SYNC,
                    SyncJob.status.in_([STATUS_PENDING, STATUS_RUNNING]),
                    SyncJob.created_at < datetime.now(UTC) - _STALE_JOB_CUTOFF,
                )
            )
            .values(status=STATUS_FAILED, finished_at=func.now())
        )
        await db.commit()

        rows_result = await db.execute(
            select(
                CompanyProviderCredentials.company_id,
                CompanyProviderCredentials.provider,
            )
            .where(CompanyProviderCredentials.active.is_(True))
            .distinct()
        )
        rows = rows_result.all()

        for company_id, provider in rows:
            inflight_result = await db.execute(
                select(SyncJob.id).where(
                    and_(
                        SyncJob.company_id == company_id,
                        SyncJob.kind == KIND_ROUTING_SYNC,
                        SyncJob.provider == provider,
                        SyncJob.status.in_([STATUS_PENDING, STATUS_RUNNING]),
                    )
                )
            )
            if inflight_result.scalar_one_or_none() is not None:
                skipped += 1
                continue

            job_id = secrets.token_urlsafe(16)
            db.add(
                SyncJob(
                    id=job_id,
                    kind=KIND_ROUTING_SYNC,
                    provider=provider,
                    company_id=company_id,
                    triggered_by=None,
                    status=STATUS_PENDING,
                    errors_json=[],
                    params_json={"provider": provider, "trigger": "cron"},
                )
            )
            await db.commit()
            created += 1

            try:
                await ctx["redis"].enqueue_job(
                    "routing_sync_for_provider",
                    job_id,
                    provider,
                    str(company_id),
                    _job_id=job_id,
                )
                queued += 1
            except Exception:
                # Don't abort the whole batch — fail just this job so it doesn't
                # linger as pending and block the next nightly run.
                logger.exception(
                    "failed to enqueue routing sync job %s (provider=%s)",
                    job_id,
                    provider,
                )
                await db.execute(
                    update(SyncJob)
                    .where(SyncJob.id == job_id)
                    .values(status=STATUS_FAILED, finished_at=func.now())
                )
                await db.commit()

    logger.info(
        "nightly_routing_sync_scheduled",
        extra={"created": created, "queued": queued, "skipped": skipped},
    )
    return {"ok": True, "created": created, "queued": queued, "skipped": skipped}
