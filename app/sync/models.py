"""SQLAlchemy ORM for the sync_jobs table (ADR-012, migration 008)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.base import Base

KIND_ROUTING_SYNC = "routing_sync"
KIND_EXPORT = "export"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"


class SyncJob(Base):
    __tablename__ = "sync_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    company_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, server_default=STATUS_PENDING)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    progress_done: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    progress_total: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    errors_json: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    params_json: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
