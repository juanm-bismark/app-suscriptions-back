"""Sync and jobs routers (ADR-012).

Two routers defined in the same file:
  - sync_router  → /v1/sync/trigger, /v1/sync/status
  - jobs_router  → /v1/jobs/{job_id}
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.identity.dependencies import get_current_profile, require_roles
from app.identity.models.profile import AppRole, Profile
from app.providers.base import Provider
from app.shared.errors import JobNotFound, SyncAlreadyRunning
from app.sync.models import (
    KIND_ROUTING_SYNC,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_RUNNING,
    SyncJob,
)
from app.sync.queue import get_arq_pool
from app.sync.schemas import (
    InFlightJob,
    JobOut,
    JobProgress,
    ProviderFreshness,
    SyncStatusOut,
    SyncTriggerOut,
)

logger = structlog.get_logger(__name__)

# A job stuck pending/running past this age is treated as abandoned (the worker
# job_timeout ceiling is 1h, so 2h means it can never legitimately still run).
_STALE_JOB_CUTOFF = timedelta(hours=2)

DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentProfile = Annotated[Profile, Depends(get_current_profile)]

sync_router = APIRouter(prefix="/sync", tags=["sync"])
jobs_router = APIRouter(prefix="/jobs", tags=["jobs"])


@sync_router.post("/trigger", response_model=SyncTriggerOut, status_code=202)
async def trigger_sync(
    provider: Provider = Query(...),
    current: Profile = Depends(require_roles(AppRole.admin)),
    db: AsyncSession = Depends(get_db),
    pool: ArqRedis = Depends(get_arq_pool),
) -> SyncTriggerOut:
    """Manually trigger a routing sync job for a provider (admin only)."""
    company_id: uuid.UUID = current.company_id  # type: ignore[assignment]

    # Self-heal abandoned jobs (enqueue that never reached the worker, or a dead
    # worker) so they stop blocking new syncs forever.
    await db.execute(
        update(SyncJob)
        .where(
            and_(
                SyncJob.company_id == company_id,
                SyncJob.kind == KIND_ROUTING_SYNC,
                SyncJob.provider == provider.value,
                SyncJob.status.in_([STATUS_PENDING, STATUS_RUNNING]),
                SyncJob.created_at < datetime.now(UTC) - _STALE_JOB_CUTOFF,
            )
        )
        .values(status=STATUS_FAILED, finished_at=func.now())
    )

    existing = await db.execute(
        select(SyncJob.id).where(
            and_(
                SyncJob.company_id == company_id,
                SyncJob.kind == KIND_ROUTING_SYNC,
                SyncJob.provider == provider.value,
                SyncJob.status.in_([STATUS_PENDING, STATUS_RUNNING]),
            )
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise SyncAlreadyRunning(detail=f"A sync for {provider} is already in progress")

    job_id = secrets.token_urlsafe(16)
    job = SyncJob(
        id=job_id,
        kind=KIND_ROUTING_SYNC,
        provider=provider.value,
        company_id=company_id,
        triggered_by=current.id,
        status=STATUS_PENDING,
        errors_json=[],
        params_json={"provider": provider.value},
    )
    db.add(job)
    await db.commit()

    try:
        await pool.enqueue_job(
            "routing_sync_for_provider",
            job_id,
            provider.value,
            str(company_id),
            _job_id=job_id,
        )
    except Exception as exc:
        # The pending row is committed; if enqueue fails, fail it now instead of
        # leaving it to block every future sync for this provider.
        await db.execute(
            update(SyncJob)
            .where(SyncJob.id == job_id)
            .values(status=STATUS_FAILED, finished_at=func.now())
        )
        await db.commit()
        logger.error(
            "sync_enqueue_failed",
            job_id=job_id,
            provider=provider.value,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Failed to enqueue sync job; the async queue is unavailable",
        ) from exc

    logger.info("sync_triggered", job_id=job_id, provider=provider.value, company_id=str(company_id))
    return SyncTriggerOut(job_id=job_id, status_url=f"/v1/jobs/{job_id}")


@sync_router.get("/status", response_model=SyncStatusOut)
async def get_sync_status(
    current: CurrentProfile,
    db: DbSession,
) -> SyncStatusOut:
    """Return per-provider freshness + current in-flight jobs for this tenant."""
    company_id: uuid.UUID = current.company_id  # type: ignore[assignment]

    done_rows = await db.execute(
        select(SyncJob)
        .where(
            and_(
                SyncJob.company_id == company_id,
                SyncJob.kind == KIND_ROUTING_SYNC,
                SyncJob.status == STATUS_DONE,
            )
        )
        .order_by(SyncJob.finished_at.desc())
    )
    seen_providers: dict[str, SyncJob] = {}
    for job in done_rows.scalars().all():
        if job.provider and job.provider not in seen_providers:
            seen_providers[job.provider] = job

    freshness = [
        ProviderFreshness(
            provider=p.value,
            last_finished_at=seen_providers[p.value].finished_at if p.value in seen_providers else None,
            last_status=seen_providers[p.value].status if p.value in seen_providers else None,
        )
        for p in Provider
    ]

    inflight_rows = await db.execute(
        select(SyncJob)
        .where(
            and_(
                SyncJob.company_id == company_id,
                SyncJob.status.in_([STATUS_PENDING, STATUS_RUNNING]),
            )
        )
        .order_by(SyncJob.created_at.desc())
    )
    in_flight = [
        InFlightJob(
            job_id=job.id,
            provider=job.provider,
            kind=job.kind,
            status=job.status,
            created_at=job.created_at,
            progress_done=job.progress_done,
            progress_total=job.progress_total,
        )
        for job in inflight_rows.scalars().all()
    ]

    return SyncStatusOut(freshness=freshness, in_flight=in_flight)


@jobs_router.get("/{job_id}", response_model=JobOut)
async def get_job(
    job_id: str,
    current: CurrentProfile,
    db: DbSession,
) -> JobOut:
    """Return a job by ID. Non-admins get 404 for cross-tenant jobs."""
    result = await db.execute(select(SyncJob).where(SyncJob.id == job_id))
    job = result.scalar_one_or_none()

    if job is None:
        raise JobNotFound(detail=f"Job {job_id!r} not found")

    if current.role != AppRole.admin and job.company_id != current.company_id:
        raise JobNotFound(detail=f"Job {job_id!r} not found")

    return JobOut(
        job_id=job.id,
        kind=job.kind,
        provider=job.provider,
        status=job.status,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        progress=JobProgress(done=job.progress_done, total=job.progress_total),
        result_url=job.result_url,
        errors=job.errors_json or [],
    )
