# Validation Report

**Status date:** 2026-05-07
**Scope:** Current repository documentation/code alignment for the Subscriptions API.
**Status:** Current summary. Supersedes the 2026-04-29 validation report.

This file used to contain an early code-review snapshot. That snapshot is no
longer reliable as a current readiness report: several findings were fixed,
some planned items were implemented, and later architecture decisions changed
the source of truth for Moabits listing/enrichment.

Use these documents for detailed decisions:

- `docs/architecture/DOCS_CODE_ALIGNMENT_AUDIT.md`
- `IMPLEMENTATION_PLAN.md`
- `docs/architecture/ARCHITECTURE.md`
- `docs/architecture/adrs/ADR-010-moabits-explicit-company-codes-bootstrap.md`
- `docs/architecture/adrs/ADR-011-moabits-v2-list-enrichment.md`

## Current Implementation Snapshot

The backend is a FastAPI multi-tenant proxy for Kite, Tele2 and Moabits under
the canonical `/v1/sims/**` facade. It does not persist canonical SIM state.
It persists routing, encrypted provider credentials/configuration, idempotency
keys and operational audit records.

Implemented and aligned with current docs:

- Provider registry for Kite, Tele2 and Moabits.
- Provider capabilities endpoint: `GET /v1/providers/{provider}/capabilities`.
- Global SIM lookup via `sim_routing_map`, avoiding default cross-provider fan-out.
- Provider-scoped SIM listing for Kite, Tele2 and Moabits.
- Canonical usage and presence endpoints.
- Lifecycle writes gated by `LIFECYCLE_WRITES_ENABLED`.
- Idempotency requirement for mutating SIM operations.
- RBAC for credential/configuration and SIM control operations.
- Fernet-encrypted provider credentials.
- Request ID middleware and structured logging.
- Circuit breaker implementation.
- Tele2 provider-specific in-process limiter.
- Moabits explicit `company_codes` bootstrap through `provider_source_configs`.
- Moabits v2 listing enrichment behind `MOABITS_V2_ENRICHMENT_ENABLED`.

## Current Known Gaps

These are the main remaining production-hardening or confirmation items:

- Generic provider-call audit table usage is still pending; lifecycle writes
  have dedicated audit, but not every provider call is captured in a generic
  `provider_call_audit` flow.
- Generic metrics/OpenTelemetry instrumentation is not yet implemented.
- Generic tenant rate limiting is not yet implemented.
- Generic cache/bulkhead policy is not implemented across every adapter.
  Tele2 and Moabits v2 have provider-specific controls.
- Moabits v2 production assumptions still require provider confirmation:
  whether v1 and v2 use the same `X-API-KEY`, max batch size and rate limit.
- Moabits v2 enrichment degradation currently lives primarily in per-SIM
  `provider_fields.enrichment_status`; decide whether to also propagate it to
  top-level `SimListOut.partial` / `failed_providers`.

## Documents Cleaned Up

The older 2026-04-29 findings about missing tests, missing DTO docs, Moabits
`to_native()` behavior and no circuit breaker are no longer valid as current
statements. They have been removed from this report to avoid contradictory
guidance.

For historical archaeology, use git history rather than treating old review
text as live project documentation.
