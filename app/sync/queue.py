"""Arq connection pool lifecycle and FastAPI dependency (ADR-012)."""

from __future__ import annotations

from typing import Annotated

import structlog
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from fastapi import Depends, HTTPException, Request, status

logger = structlog.get_logger(__name__)


async def init_arq_pool(redis_url: str) -> ArqRedis:
    settings = RedisSettings.from_dsn(redis_url)
    pool = await create_pool(settings)
    logger.info("arq_pool_initialized", redis_url=redis_url)
    return pool


async def close_arq_pool(pool: ArqRedis) -> None:
    await pool.aclose()
    logger.info("arq_pool_closed")


async def get_arq_pool(request: Request) -> ArqRedis:
    pool: ArqRedis | None = getattr(request.app.state, "arq_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Async queue unavailable",
        )
    return pool


ArqPoolDep = Annotated[ArqRedis, Depends(get_arq_pool)]
