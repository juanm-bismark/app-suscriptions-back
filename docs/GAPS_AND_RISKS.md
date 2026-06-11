# Gaps and Risks — Operational and Testing Assessment

**Date:** 2026-06-11  
**Scope:** findings from end-to-end logic review + provider API spec cross-check (Kite UNICA SOAP v12, Tele2 Cisco Control Center REST).

Gaps are ordered by the cost of discovering them in production vs. in development.

---

## P0 — Find before production load

### No integration tests against provider APIs

**Risk:** Unit tests mock the adapter layer, so bugs in the actual HTTP/SOAP call construction pass all tests undetected. This session found two examples:

- Kite search used `lifeCycleStatus` (wrong) instead of `lifeCycleState`, and `customField1` instead of `customField_1`. All unit tests passed.
- Moabits routing sync was returning 0 SIMs silently because `company_code` was not injected into credentials. The adapter returned `([], None)` immediately and no test caught it.

**What's needed:** HTTP-level stubs (e.g., WireMock, `pytest-httpx` recording mode, or hand-crafted fixtures) that capture real SOAP envelope structures and REST response shapes from each provider. At minimum: one happy-path listing test per provider at the HTTP level, not at the adapter method level.

**Files at risk:** [app/providers/kite/adapter.py](../app/providers/kite/adapter.py), [app/providers/tele2/adapter.py](../app/providers/tele2/adapter.py), [app/providers/moabits/adapter.py](../app/providers/moabits/adapter.py), [app/sync/tasks.py](../app/sync/tasks.py).

---

## P1 — Will hurt at moderate scale

### In-process circuit breaker and TPS limiter

**Risk:** The circuit breaker (`app/shared/resilience.py`) and the Tele2 TPS limiter (`app/providers/tele2/adapter.py`) live in per-process memory. With multiple uvicorn workers or container replicas:

- A burst of failures through worker A opens A's breaker but not B's — one worker fast-fails while the other continues hammering the provider.
- Tele2's `max_tps=1` (Cisco policy) is silently violated by factor N if N workers run. This can trigger API suspension by Cisco.
- The stale-job self-heal cutoff (`_STALE_JOB_CUTOFF = 2h`) assumes a single scheduler; with multiple scheduler processes, dedup breaks.

**Fix:** Redis-backed circuit breaker state and rate limiter (Lua atomic script or `slowapi` with Redis backend) before running more than 1 worker or replica. ADR-005 already documents the migration trigger.

**Files:** [app/shared/resilience.py](../app/shared/resilience.py), [app/providers/tele2/adapter.py](../app/providers/tele2/adapter.py).

### No visibility into provider health

**Risk:** The circuit breaker state (`CLOSED/OPEN/HALF_OPEN`) is in-memory and unexported. If a provider degrades, the first signal is user complaints — there is no automated alert and no way to query current breaker state without reading memory directly.

**Minimum viable fix:** A `/metrics` endpoint exposing `circuit_breaker_state{provider}` (0=closed, 1=open, 2=half-open) and `provider_request_duration_seconds{provider,operation,outcome}`. `prometheus-fastapi-instrumentator` handles the HTTP layer; the adapter base needs a custom counter for provider-level outcomes.

This is NFR-O3/O4 from [nfr-analysis.md](architecture/nfr-analysis.md), still at `plan` status.

---

## P2 — Design debt, already documented in ADRs

### No retry for transient failures (ADR-005)

A brief network hiccup or a provider 503 flap always surfaces as an error to the client. `tenacity` on idempotent GET operations is the documented approach. Not implemented. Avoid on mutations until provider idempotency is confirmed.

### No L1 cache / stampede protection for Kite and Moabits (ADR-005)

Concurrent requests for the same ICCID hit the provider N times. Tele2 has an in-process serializer; Kite and Moabits do not. `cachetools.TTLCache` with a single-flight asyncio.Future pattern is the documented fix.

### Generic audit middleware absent (ADR-008 / NFR-Sec4)

`audit_log` and `lifecycle_change_audit` tables exist. `lifecycle_change_audit` is written by SIM write operations. But credential mutations and 403 access denials are not audited — the middleware/decorator described in ADR-008 was never implemented.

---

## P3 — Tracked, low urgency

### POST /v1/sims/export not implemented

Deferred in ADR-012. The job schema, arq task registration, and worker scaffolding exist; only the adapter fan-out and S3/object-storage upload step are missing.

### Python 3.14 ElementTree truthiness

`element.find(...)` returns `None` or an Element. In Python ≤ 3.13, `element or fallback` silently works because empty Elements are falsy; in 3.14 this raises a `DeprecationWarning` (and later an error). `app/providers/kite/adapter.py` was fixed this session to use explicit `is None` guards. Watch for the same pattern if new Kite XML parsing code is added.

---

## Summary table

| Priority | Gap | Owner | Blocking prod? |
|---|---|---|---|
| P0 | Provider integration test stubs | backend | yes — bugs found only in spec review |
| P1 | Redis-backed breaker + TPS limiter | backend + infra | yes — before horizontal scale |
| P1 | `/metrics` with breaker state | backend | yes — no alert without it |
| P2 | Retry with backoff (GET only) | backend | no — UX degradation |
| P2 | L1 cache + single-flight | backend | no — provider quota risk at peak |
| P2 | Generic audit middleware | backend | no — compliance gap |
| P3 | `/sims/export` implementation | backend | no — deferred by ADR-012 |
