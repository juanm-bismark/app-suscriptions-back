"""Arq worker entrypoint for async jobs (ADR-012).

This module defines the WorkerSettings consumed by `python -m arq
app.sync.worker.WorkerSettings`.

Redis is broker only; SIM state is never cached here (ADR-002 preserved).
"""

from __future__ import annotations

import logging
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from app.config import get_settings
from app.sync.tasks import routing_sync_for_provider, schedule_nightly_routing_sync

logger = logging.getLogger(__name__)


def _cron_time_from_env() -> tuple[int, int]:
    """Parse the minute/hour from a standard 5-field cron expression."""
    expr = get_settings().sync_cron_expr
    parts = expr.split()
    if len(parts) != 5:
        logger.warning("invalid_sync_cron_expr", extra={"expr": expr})
        return 0, 2
    try:
        minute = int(parts[0])
        hour = int(parts[1])
    except ValueError:
        logger.warning("unsupported_sync_cron_expr", extra={"expr": expr})
        return 0, 2
    if not (0 <= minute <= 59 and 0 <= hour <= 23):
        logger.warning("invalid_sync_cron_time", extra={"expr": expr})
        return 0, 2
    return minute, hour


def _redis_settings_from_env() -> RedisSettings:
    settings = get_settings()
    if not settings.redis_url:
        raise RuntimeError(
            "REDIS_URL is not configured. Set it in .env or docker-compose env."
        )
    return RedisSettings.from_dsn(settings.redis_url)


async def noop(ctx: dict[str, Any], message: str = "ping") -> dict[str, Any]:
    """Smoke-test task — validates API ↔ Redis ↔ Worker wiring."""
    job_id = ctx.get("job_id", "<unknown>")
    logger.info("noop task executed", extra={"job_id": job_id, "message": message})
    return {"ok": True, "echo": message, "job_id": job_id}


async def on_startup(ctx: dict[str, Any]) -> None:
    """Initialize DB engine and provider registry in the worker process."""
    from app.config import require_database_url
    from app.database import init_engine
    from app.providers.base import Provider
    from app.providers.kite.adapter import KiteAdapter
    from app.providers.moabits.adapter import MoabitsAdapter
    from app.providers.registry import ProviderRegistry
    from app.providers.tele2.adapter import Tele2Adapter

    settings = get_settings()
    init_engine(require_database_url(settings), echo=settings.database_echo)

    registry = ProviderRegistry()
    registry.register(Provider.KITE, KiteAdapter())
    registry.register(Provider.TELE2, Tele2Adapter())
    registry.register(Provider.MOABITS, MoabitsAdapter())
    ctx["registry"] = registry

    logger.info("arq worker starting", extra={"worker_id": id(ctx)})


async def on_shutdown(ctx: dict[str, Any]) -> None:
    """Close DB engine on worker shutdown."""
    from app.database import close_engine

    await close_engine()
    logger.info("arq worker shutting down")


class WorkerSettings:
    """Arq worker config. See https://arq-docs.helpmanual.io/ for reference."""

    redis_settings = _redis_settings_from_env()

    functions = [noop, routing_sync_for_provider, schedule_nightly_routing_sync]

    on_startup = on_startup
    on_shutdown = on_shutdown

    _cron_minute, _cron_hour = _cron_time_from_env()
    cron_jobs = [
        cron(
            schedule_nightly_routing_sync,
            name="nightly-routing-sync",
            hour=_cron_hour,
            minute=_cron_minute,
            unique=True,
        )
    ]

    max_jobs = 10
    job_timeout = 60 * 60  # 1h ceiling; export jobs may need longer in fase D
    keep_result = 60 * 60  # keep result in redis 1h for clients polling /v1/jobs/{id}
