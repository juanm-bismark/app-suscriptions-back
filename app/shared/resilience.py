"""In-process per-adapter circuit breaker and TTL cache.

Circuit breaker states:
  CLOSED  — normal operation; failures are counted.
  OPEN    — fast-fail; no calls forwarded to provider.
  HALF_OPEN — one probe call allowed; resets on success or reopens on failure.

Parameters (from ADR-005):
  failure_threshold  : open after this many failures in the window  (default 5)
  failure_window_s   : rolling window for counting failures          (default 30)
  open_duration_s    : how long the breaker stays open              (default 30)
"""

import asyncio
import math
import time
from collections import deque
from collections.abc import Awaitable, Callable
from enum import Enum
from functools import wraps
from typing import Any, TypeVar

from app.shared.errors import ProviderUnavailable

T = TypeVar("T")


class _State(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        failure_window_s: float = 30.0,
        open_duration_s: float = 30.0,
    ) -> None:
        self.name = name
        self._threshold = failure_threshold
        self._window = failure_window_s
        self._open_duration = open_duration_s

        self._state = _State.CLOSED
        self._failures: deque[float] = deque()
        self._failure_count = 0
        self._success_count = 0
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state.value

    def state_info(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "state": self.state,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
        }

    async def call(
        self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any
    ) -> T:
        async with self._lock:
            now = time.monotonic()

            if self._state == _State.OPEN:
                remaining_open_s = self._open_duration - (now - self._opened_at)
                if remaining_open_s <= 0:
                    self._state = _State.HALF_OPEN
                else:
                    raise ProviderUnavailable(
                        detail=f"circuit breaker is OPEN for provider '{self.name}'",
                        retry_after=math.ceil(remaining_open_s),
                    )

        try:
            result = await fn(*args, **kwargs)
        except Exception:
            await self._record_failure()
            raise
        else:
            await self._record_success()
            return result

    async def _record_success(self) -> None:
        async with self._lock:
            if self._state == _State.HALF_OPEN:
                self._state = _State.CLOSED
                self._failures.clear()
            self._success_count += 1

    async def _record_failure(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._failures.append(now)
            self._failure_count += 1
            cutoff = now - self._window
            while self._failures and self._failures[0] < cutoff:
                self._failures.popleft()

            if (
                self._state == _State.HALF_OPEN
                or len(self._failures) >= self._threshold
            ):
                self._state = _State.OPEN
                self._opened_at = now
                self._failures.clear()


def with_circuit_breaker(
    breaker: CircuitBreaker,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator that wraps an async method with the given circuit breaker."""

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            return await breaker.call(fn, *args, **kwargs)

        return wrapper

    return decorator
