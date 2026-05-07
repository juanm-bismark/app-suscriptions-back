# Testing Strategy — Subscriptions API

**Status date:** 2026-05-07
**ADR:** `docs/architecture/adrs/ADR-009-testing-strategy.md`

The suite follows a fast test pyramid: domain/schema unit tests, provider
mapper/adapter tests with mocked HTTP, router/component tests with fake
dependencies, and resilience/control-flow tests. Real provider sandbox tests are
not part of the default suite.

## Current Tree

```text
tests/
├── conftest.py
├── test_domain.py
├── test_schemas.py
├── test_provider_capabilities.py
├── test_credentials_router.py
├── test_sims_router_controls.py
├── test_resilience.py
└── providers/
    ├── test_provider_contract.py
    ├── test_kite_adapter.py
    ├── test_kite_faults.py
    ├── test_kite_mappers.py
    ├── test_kite_writes.py
    ├── test_tele2_adapter.py
    ├── test_tele2_status_map.py
    ├── test_moabits_adapter.py
    └── test_moabits_writes.py
```

## Coverage Areas

Current tests cover:

- Domain enums and value objects.
- Pydantic API schemas.
- Provider capability reporting.
- Credential router behavior, including Moabits company-code controls.
- SIM router controls, idempotency and provider resolution.
- Provider contract conformance.
- Kite mapper, fault and write behavior.
- Tele2 adapter/status behavior.
- Moabits adapter, usage, listing and write behavior.
- Resilience helpers such as circuit breaker behavior.

## Mocking Strategy

Provider tests use local fixtures/mocks and `pytest-httpx` / `pytest-asyncio`
from `requirements-dev.txt`. Adapter behavior tests are no longer broadly
skipped pending HTTP mocking setup.

Real provider payloads should be added as golden fixtures when captured from
Kite, Tele2 or Moabits. Those fixtures should verify mapper drift without
requiring live provider credentials in normal CI.

## Running Tests

Install development dependencies, then run:

```bash
pytest tests/
```

Useful focused runs:

```bash
pytest tests/test_domain.py -v
pytest tests/test_sims_router_controls.py -v
pytest tests/providers/test_moabits_adapter.py -v
pytest tests/providers/test_provider_contract.py -v
```

Coverage:

```bash
pytest tests/ --cov=app --cov-report=term-missing
```

## Remaining Test Gaps

- Live sandbox tests are intentionally absent until providers supply stable
  sandbox access and rate limits.
- Moabits v2 enrichment should keep tests for full, detail-only,
  connectivity-only, timeout and batch-boundary behavior as the provider
  contract is confirmed.
- Metrics/OpenTelemetry and generic provider-call audit need tests when those
  features are implemented.
