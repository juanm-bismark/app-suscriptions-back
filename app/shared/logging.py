"""Structured logging setup using structlog.

Call setup_logging() once at application startup (lifespan).
All modules use structlog.get_logger() — the configuration here applies globally.
"""

import logging

import structlog
from structlog.types import Processor as StructlogProcessor


_NOISY_TRANSPORT_LOGGERS = (
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "hpack",
)


def _silence_transport_loggers() -> None:
    for logger_name in _NOISY_TRANSPORT_LOGGERS:
        logger = logging.getLogger(logger_name)
        logger.handlers.clear()
        logger.setLevel(logging.WARNING)
        logger.propagate = False


def setup_logging(environment: str = "development") -> None:
    shared_processors: list[StructlogProcessor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    renderer: StructlogProcessor
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
        force=True,
    )

    # httpx/httpcore emit per-socket debug traces when the root logger is DEBUG.
    # Keep application debug logs useful without flooding provider request output.
    _silence_transport_loggers()
