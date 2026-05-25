"""Pydantic schemas for sync / job endpoints (ADR-012)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class SyncTriggerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    status_url: str


class ProviderFreshness(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    last_finished_at: datetime | None
    last_status: str | None


class InFlightJob(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    provider: str | None
    kind: str
    status: str
    created_at: datetime
    progress_done: int
    progress_total: int | None


class SyncStatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    freshness: list[ProviderFreshness]
    in_flight: list[InFlightJob]


class JobProgress(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    done: int
    total: int | None


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: str
    kind: str
    provider: str | None
    status: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    progress: JobProgress
    result_url: str | None
    errors: list
