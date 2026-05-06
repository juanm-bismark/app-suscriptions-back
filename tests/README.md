# Testing Strategy — Subscriptions API

**Status**: 2026-04-30 | Test pyramid structure per ADR-009

## Overview

The test suite follows a **testing pyramid** strategy (ADR-009) with three layers:

1. **Unit Tests** — Fast, isolated tests of individual components
2. **Contract Tests** — Validate adapter protocol compliance
3. **Integration Tests** — End-to-end API flows (marked `@pytest.mark.skip` for CI)

## Directory Structure

```
tests/
├── __init__.py
├── conftest.py                    # Shared fixtures
├── test_domain.py                 # Domain model tests
├── test_schemas.py                # Pydantic schema tests
└── providers/
    ├── __init__.py
    ├── test_kite_adapter.py        # Kite status mapping + behavior
    ├── test_tele2_adapter.py       # Tele2 status mapping + behavior
    └── test_moabits_adapter.py     # Moabits adapter (unit + fixtures)
```

## Test Categories

### Layer 1: Unit Tests (Fast, Mocked)

**Domain Model** (`test_domain.py`):
- Enum correctness (AdministrativeStatus, ConnectivityState)
- Aggregate immutability (Subscription)
- Value object creation (UsageSnapshot, ConnectivityPresence)

**Status Mappings** (`providers/test_*.py`):
- Bidirectional mapping: `native ↔ canonical`
- Coverage of all provider states
- Fallback to UNKNOWN for unrecognized values
- Reverse mapping validation (unsupported transitions raise UnsupportedOperation)

**Schemas** (`test_schemas.py`):
- Pydantic model validation
- OpenAPI enum typing for `AdministrativeStatus`
- `from_attributes=True` configuration (ORM compatibility)
- Optional field handling (data_service, sms_service in StatusChangeIn)

### Layer 2: Adapter Behavior Tests (Mocked HTTP)

Marked with `@pytest.mark.skip` pending `responses` or `httpx-mock` setup.

**Kite Adapter**:
- SOAP/XML parsing for getSubscriptionDetail
- Consumption data extraction (daily/monthly blocks)
- Presence level handling (known levels → logging for unknowns)
- UnsupportedOperation for lifecycle changes

**Tele2 Adapter**:
- REST/JSON parsing for device endpoints
- date_activated / date_modified extraction
- Idempotency-Key header forwarding
- purge() delegation to set_administrative_status(PURGED)
- HTTP 404 detection via exc.detail
- Cisco/Jasper Search Devices contract:
  - required `modifiedSince`
  - strict `yyyy-MM-ddTHH:mm:ssZ`
  - max one-year lookback
  - `pageSize` clamp/default 50 and `modifiedTill = modifiedSince + 1 year`
- Cisco fair-use handling:
  - `40000029 Rate Limit Exceeded` maps to `ProviderRateLimited`
  - Tele2 credentials can carry `account_scope.max_tps` for Advantage-style 5 TPS
  - in-process limiter serializes Tele2 calls per account key

**Moabits Adapter**:
- Parallel API calls (getSimDetails + getServiceStatus)
- services normalization (slash-separated → list)
- Selective data/SMS service control
- Local pagination warning + behavior
- Idempotency-Key header forwarding

### Layer 3: Integration Tests (Optional, E2E)

Not included in current suite; can be added if provider sandboxes become available.

## Running Tests

### All tests:
```bash
pytest tests/
```

### Only unit tests (skip skipped tests):
```bash
pytest tests/ -m "not skip"
```

### Specific file:
```bash
pytest tests/test_domain.py -v
pytest tests/providers/test_kite_adapter.py -v
```

### Coverage:
```bash
pytest tests/ --cov=app --cov-report=term-missing
```

## Next Steps: Mocking Setup

To unlock Layer 2 tests, install and configure:

```bash
pip install responses httpx-mock pytest-asyncio
```

Then update fixtures:
```python
@pytest.mark.asyncio
async def test_get_subscription_parses_soap(moabits_creds):
    """Mock SOAP response and test parsing."""
    from responses import matchers, mock

    with mock.mock():
        # Register responses, call adapter, assert results
        pass
```

## Future: Contract Tests

Once API documentation is final, consider:
- `pact` library for provider contract testing
- Golden files for provider payloads (verified against spec)
- Snapshot tests for Subscription serialization

## Known Issues

**Layer 2 tests currently skipped** due to:
- Lack of mock HTTP client setup in CI
- Complexity of mocking async httpx calls
- SOAP/XML parsing requires realistic payload samples

**Recommendation**: Set up `pytest-asyncio` + `httpx-mock` in next phase.
