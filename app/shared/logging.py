"""Structured logging setup using structlog.

Call setup_logging() once at application startup (lifespan).
All modules use structlog.get_logger() — the configuration here applies globally.
"""

import logging

import structlog
from structlog.types import Processor as StructlogProcessor


def setup_logging(environment: str = "development") -> None:
    shared_processors: list[StructlogProcessor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if environment == "development":
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    processors_list: list[StructlogProcessor] = [
        *shared_processors,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        renderer,
    ]

    structlog.configure(
        processors=processors_list,
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging through structlog so uvicorn/SQLAlchemy logs are captured
    logging.basicConfig(
        format="%(message)s",
        level=logging.INFO if environment != "development" else logging.DEBUG,
    )
