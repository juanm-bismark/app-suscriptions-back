from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings, require_database_url
from app.database import close_engine, init_engine
from app.identity.routers import auth, me, users
from app.providers import routers as provider_routers
from app.providers.base import Provider
from app.providers.kite.adapter import KiteAdapter
from app.providers.moabits.adapter import MoabitsAdapter
from app.providers.registry import ProviderRegistry
from app.providers.tele2.adapter import Tele2Adapter
from app.shared.errors import DomainError
from app.shared.logging import setup_logging
from app.shared.middleware import RequestIDMiddleware
from app.subscriptions.routers import sims
from app.tenancy.routers import companies, credentials

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.environment)
    init_engine(require_database_url(settings), echo=settings.database_echo)

    registry = ProviderRegistry()
    registry.register(Provider.KITE, KiteAdapter())
    registry.register(Provider.TELE2, Tele2Adapter())
    registry.register(Provider.MOABITS, MoabitsAdapter())
    app.state.provider_registry = registry

    logger.info("startup", environment=settings.environment, providers=registry.registered_providers())
    yield
    await close_engine()
    logger.info("shutdown")


settings = get_settings()

app = FastAPI(title="Subscriptions API", version="1.0.0", lifespan=lifespan)

app.add_middleware(RequestIDMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    logger.warning(
        "domain_error",
        code=exc.code,
        status=exc.http_status,
        detail=exc.detail,
        **exc.extra,
    )
    return JSONResponse(
        status_code=exc.http_status,
        media_type="application/problem+json",
        content={
            "type": f"https://api.example.com/errors/{exc.code}",
            "title": exc.title,
            "status": exc.http_status,
            "code": exc.code,
            "detail": exc.detail,
            "instance": request.headers.get("X-Request-ID"),
            **exc.extra,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_exception", exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        media_type="application/problem+json",
        content={
            "type": "https://api.example.com/errors/internal_error",
            "title": "Internal server error",
            "status": 500,
            "code": "internal_error",
            "detail": None,
            "instance": request.headers.get("X-Request-ID"),
        },
    )


app.include_router(auth.router, prefix="/v1")
app.include_router(me.router, prefix="/v1")
app.include_router(users.router, prefix="/v1")
app.include_router(companies.router, prefix="/v1")
app.include_router(credentials.router, prefix="/v1")
app.include_router(sims.router, prefix="/v1")
app.include_router(provider_routers.router, prefix="/v1")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok"}


@app.get("/ready")
async def ready() -> dict[str, Any]:
    from sqlalchemy import text

    from app.database import get_db

    async for db in get_db():
        await db.execute(text("SELECT 1"))
    return {"status": "ready"}
