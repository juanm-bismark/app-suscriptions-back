"""Base adapter class with circuit breaker support (ADR-005)."""

from typing import Any, Awaitable, Callable, TypeVar

from app.shared.resilience import CircuitBreaker

T = TypeVar("T")


class BaseAdapter:
    """Base class for all provider adapters with circuit breaker support."""

    def __init__(self, provider_name: str):
        self.provider_name = provider_name
        # Circuit breaker: open after 5 failures in 30s, stay open for 30s
        self.circuit_breaker = CircuitBreaker(
            name=provider_name,
            failure_threshold=5,
            failure_window_s=30.0,
            open_duration_s=30.0,
        )

    async def _call_with_breaker(
        self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any
    ) -> T:
        """Execute a provider call with circuit breaker protection."""
        return await self.circuit_breaker.call(fn, *args, **kwargs)
