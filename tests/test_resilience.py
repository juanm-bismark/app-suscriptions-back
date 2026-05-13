"""Tests for circuit breaker resilience pattern (ADR-005).

Tests verify:
1. Circuit breaker state transitions (CLOSED → OPEN → HALF_OPEN → CLOSED)
2. Failure threshold triggers OPEN state
3. ProviderUnavailable exception raised when OPEN
4. Recovery after open timeout with HALF_OPEN probe
"""

import asyncio
import pytest

from app.shared.errors import ProviderUnavailable
from app.shared.resilience import CircuitBreaker


CLOSED = "CLOSED"
OPEN = "OPEN"


class TestCircuitBreaker:
    """Test circuit breaker state machine and failure handling."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_initial_state_closed(self):
        """Circuit breaker starts in CLOSED state."""
        cb = CircuitBreaker("test_provider")
        assert cb.state == CLOSED

    @pytest.mark.asyncio
    async def test_circuit_breaker_successful_call_remains_closed(self):
        """Successful call keeps breaker in CLOSED state."""
        cb = CircuitBreaker("test_provider")

        async def success_fn():
            return "ok"

        result = await cb.call(success_fn)
        assert result == "ok"
        assert cb.state == CLOSED

    @pytest.mark.asyncio
    async def test_circuit_breaker_opens_after_threshold_failures(self):
        """Circuit breaker opens after N failures in window."""
        cb = CircuitBreaker("test_provider", failure_threshold=3)

        async def fail_fn():
            raise ValueError("test error")

        # First 2 failures: stay CLOSED
        for _ in range(2):
            with pytest.raises(ValueError):
                await cb.call(fail_fn)
        assert cb.state == CLOSED

        # 3rd failure: transition to OPEN
        with pytest.raises(ValueError):
            await cb.call(fail_fn)
        assert cb.state == OPEN

    @pytest.mark.asyncio
    async def test_circuit_breaker_rejects_calls_when_open(self):
        """When OPEN, circuit breaker rejects calls with ProviderUnavailable."""
        cb = CircuitBreaker("test_provider", failure_threshold=1)

        async def fail_fn():
            raise ValueError("test error")

        # Trigger OPEN state
        with pytest.raises(ValueError):
            await cb.call(fail_fn)
        assert cb.state == OPEN

        # Subsequent call should raise ProviderUnavailable without calling fail_fn
        with pytest.raises(ProviderUnavailable) as exc_info:
            await cb.call(fail_fn)
        assert "circuit breaker is OPEN" in exc_info.value.detail
        assert exc_info.value.extra["retry_after"] == "30"

    @pytest.mark.asyncio
    async def test_circuit_breaker_open_error_includes_remaining_retry_after(self):
        """OPEN rejection tells callers when to retry."""
        cb = CircuitBreaker(
            "test_provider",
            failure_threshold=1,
            open_duration_s=0.2,
        )

        async def fail_fn():
            raise ValueError("test error")

        with pytest.raises(ValueError):
            await cb.call(fail_fn)

        await asyncio.sleep(0.05)

        with pytest.raises(ProviderUnavailable) as exc_info:
            await cb.call(fail_fn)
        assert exc_info.value.extra["retry_after"] == "1"

    @pytest.mark.asyncio
    async def test_circuit_breaker_transitions_to_half_open_after_timeout(self):
        """After open_duration, breaker transitions to HALF_OPEN."""
        cb = CircuitBreaker(
            "test_provider",
            failure_threshold=1,
            open_duration_s=0.1,  # 100ms timeout
        )

        async def fail_fn():
            raise ValueError("test error")

        # Trigger OPEN state
        with pytest.raises(ValueError):
            await cb.call(fail_fn)
        assert cb.state == OPEN

        # Wait for open timeout
        await asyncio.sleep(0.15)

        async def success_fn():
            return "recovered"

        # Should allow call and transition to HALF_OPEN, then CLOSED on success
        result = await cb.call(success_fn)
        assert result == "recovered"
        assert cb.state == CLOSED

    @pytest.mark.asyncio
    async def test_circuit_breaker_half_open_reopens_on_failure(self):
        """In HALF_OPEN state, a failure reopens the breaker."""
        cb = CircuitBreaker(
            "test_provider",
            failure_threshold=1,
            open_duration_s=0.1,
        )

        async def fail_fn():
            raise ValueError("test error")

        # Trigger OPEN state
        with pytest.raises(ValueError):
            await cb.call(fail_fn)
        assert cb.state == OPEN

        # Wait for open timeout
        await asyncio.sleep(0.15)

        # In HALF_OPEN, next failure reopens
        with pytest.raises(ValueError):
            await cb.call(fail_fn)
        assert cb.state == OPEN

    @pytest.mark.asyncio
    async def test_circuit_breaker_closes_on_half_open_success(self):
        """Successful probe in HALF_OPEN closes the breaker."""
        cb = CircuitBreaker(
            "test_provider",
            failure_threshold=1,
            open_duration_s=0.1,
        )

        async def fail_fn():
            raise ValueError("test error")

        async def success_fn():
            return "ok"

        # Trigger OPEN state
        with pytest.raises(ValueError):
            await cb.call(fail_fn)

        # Wait for open timeout
        await asyncio.sleep(0.15)

        # Successful call in HALF_OPEN → CLOSED
        result = await cb.call(success_fn)
        assert result == "ok"
        assert cb.state == CLOSED

    @pytest.mark.asyncio
    async def test_circuit_breaker_state_info(self):
        """state_info() returns current circuit breaker state for monitoring."""
        cb = CircuitBreaker("test_provider")
        info = cb.state_info()

        assert info["provider"] == "test_provider"
        assert info["state"] == CLOSED
        assert info["failure_count"] == 0
        assert info["success_count"] == 0


class TestCircuitBreakerIntegration:
    """Integration tests with mock adapter methods."""

    @pytest.mark.asyncio
    async def test_adapter_circuit_breaker_wrapping(self):
        """Verify circuit breaker wrapping on adapter methods."""
        from app.providers.moabits.adapter import MoabitsAdapter

        adapter = MoabitsAdapter()
        assert adapter.circuit_breaker.state == CLOSED
        assert adapter.provider_name == "moabits"

    @pytest.mark.asyncio
    async def test_multiple_adapters_independent_breakers(self):
        """Each adapter has independent circuit breaker state."""
        from app.providers.kite.adapter import KiteAdapter
        from app.providers.tele2.adapter import Tele2Adapter
        from app.providers.moabits.adapter import MoabitsAdapter

        kite = KiteAdapter()
        tele2 = Tele2Adapter()
        moabits = MoabitsAdapter()

        assert kite.circuit_breaker.state == CLOSED
        assert tele2.circuit_breaker.state == CLOSED
        assert moabits.circuit_breaker.state == CLOSED

        # Verify unique instances
        assert kite.circuit_breaker is not tele2.circuit_breaker
        assert tele2.circuit_breaker is not moabits.circuit_breaker
