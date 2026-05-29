"""Idempotency-key management and lifecycle audit helpers."""
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Header
from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.errors import IdempotencyKeyRequired
from app.subscriptions.models.lifecycle_audit import LifecycleChangeAudit
from app.tenancy.models.idempotency import IdempotencyKey


def _require_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if idempotency_key is None:
        raise IdempotencyKeyRequired()
    return idempotency_key


async def _claim_idempotency_key(
    key: str,
    company_id: uuid.UUID,
    db: AsyncSession,
) -> bool:
    """Atomically claim an idempotency key.

    Returns False when this company/key already exists and the provider call
    must not be repeated.
    """
    stmt = (
        pg_insert(IdempotencyKey)
        .values(
            key=key,
            response={"status": "processing"},
            company_id=company_id,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        .on_conflict_do_nothing(index_elements=["company_id", "key"])
        .returning(IdempotencyKey.id)
    )
    result = await db.execute(stmt)
    claimed = result.scalar_one_or_none() is not None
    await db.commit()
    return claimed


async def _mark_idempotency_processed(
    key: str,
    company_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    await db.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.key == key, IdempotencyKey.company_id == company_id)
        .values(response={"status": 204})
    )
    await db.commit()


async def _release_idempotency_key(
    key: str,
    company_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    await db.execute(
        delete(IdempotencyKey).where(
            IdempotencyKey.key == key,
            IdempotencyKey.company_id == company_id,
            IdempotencyKey.response == {"status": "processing"},
        )
    )
    await db.commit()


async def _write_lifecycle_audit(
    db: AsyncSession,
    *,
    company_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    request_id: str | None,
    iccid: str,
    provider: str,
    action: str,
    target: str | None,
    idempotency_key: str | None,
    outcome: str,
    latency_ms: int | None,
    provider_request_id: str | None,
    provider_error_code: str | None,
    error: str | None,
) -> None:
    record = LifecycleChangeAudit(
        company_id=str(company_id),
        actor_id=str(actor_id) if actor_id else None,
        request_id=request_id,
        iccid=iccid,
        provider=provider,
        action=action,
        target=target,
        idempotency_key=idempotency_key,
        accepted_at=datetime.now(UTC) if outcome == "success" else None,
        outcome=outcome,
        latency_ms=latency_ms,
        provider_request_id=provider_request_id,
        provider_error_code=provider_error_code,
        error=error,
    )
    db.add(record)
    await db.commit()
