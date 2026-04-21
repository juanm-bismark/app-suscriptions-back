from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database import close_engine, init_engine
from app.routers import auth, companies, me, users


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_engine(settings.database_url, echo=settings.environment == "development")
    yield
    await close_engine()


app = FastAPI(title="App Suscripciones API", version="1.0.0", lifespan=lifespan)

# Restrict CORS_ORIGINS in production via environment variable
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


app.include_router(auth.router)
app.include_router(me.router)
app.include_router(users.router)
app.include_router(companies.router)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok"}
