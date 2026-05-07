# Docs ↔ Code Alignment Audit

**Date:** 2026-05-07  
**Scope:** documentation under `docs/`, top-level planning/reference docs, and current backend code.

This audit separates current implementation facts from older architecture
phase notes. Some documents are intentionally historical; those should not
be read as live implementation status unless they now include an explicit
status note.

## Current Implementation Facts

| Area | Actual code status |
|---|---|
| App shape | Modular FastAPI app with context packages: `identity`, `tenancy`, `subscriptions`, `providers`, `shared`. |
| Public prefix | All domain/auth/tenancy/provider routers are mounted under `/v1`; `/health` and `/ready` stay unversioned. |
| Providers | `kite`, `tele2`, and `moabits` adapters are registered at startup. |
| Required provider interface | `get_subscription`, `get_usage`, `get_presence`, `set_administrative_status`, `purge`. |
| Optional listing capability | `SearchableProvider.list_subscriptions(credentials, cursor, limit, filters)` implemented by Kite, Tele2, and Moabits. |
| Capabilities endpoint | `GET /v1/providers/{provider}/capabilities` is implemented. |
| Routing map | `sim_routing_map` is persisted and used for single-SIM/global listing routing. |
| Credentials | `company_provider_credentials.credentials_enc` stores encrypted provider secrets with Fernet. |
| Moabits source scope | `provider_source_configs.settings.company_codes`, not `credentials_enc`, is the source of truth for selected Moabits company codes. |
| Moabits v2 enrichment | Implemented for provider-scoped listing behind `MOABITS_V2_ENRICHMENT_ENABLED`. |
| Lifecycle writes | `PUT /v1/sims/{iccid}/status` and `POST /v1/sims/{iccid}/purge` are implemented, admin-only, idempotency-key required, and gated in adapters by `LIFECYCLE_WRITES_ENABLED`. |
| Lifecycle audit | Writes/replays/failures are recorded in `lifecycle_change_audit`. |
| Generic audit | `audit_log` migration exists, but there is no implemented generic audit middleware/decorator for every credential mutation or 403 denial. |
| Circuit breaker | Implemented in `BaseAdapter` / `CircuitBreaker` for all adapters. |
| Generic cache / bulkhead | Not implemented generally. Tele2 has a provider-specific in-process TPS limiter; Moabits v2 enrichment has a per-request chunk semaphore. |
| Request ID | `RequestIDMiddleware` echoes/binds `X-Request-ID`. |
| Logging | `structlog` is configured; JSON rendering is used outside development. |
| Metrics / tracing / rate limiting | No `/metrics`, Prometheus instrumentation, OpenTelemetry tracing, or tenant token bucket is implemented yet. |
| Refresh tokens | Stored as sha256 hex digest, not raw token. |
| CORS | Uses explicit `settings.cors_origins`, defaulting to local dev origins. |

## Document-by-Document Review

| Document | Status | Notes / gaps |
|---|---|---|
| `docs/architecture/ARCHITECTURE.md` | Partially current | Updated with ADR-011. Still mixes target architecture with implementation roadmap; remaining Phase/Ola sections should be treated as roadmap, not current state. |
| `docs/architecture/domain-model.md` | Updated | Corrected current status for contexts, `provider_source_configs`, actual enum values, `ConnectivityPresence`, and HTTP-derived `detail_level` / `normalized`. |
| `docs/architecture/nfr-analysis.md` | Needs follow-up | Many NFR rows remain target-state. Actual implemented: CORS, refresh-token hashing, request-id middleware, structlog setup, circuit breaker, lifecycle audit. Missing: metrics, OTel, tenant rate limit, generic audit middleware, generic cache/bulkhead. |
| `docs/architecture/patterns-decisions.md` | Historical design phase | Added explicit historical note. Keep as design rationale; do not use alone to infer current implementation completeness. |
| `docs/architecture/arch-analysis.md` | Historical snapshot | Added note that it describes the pre-provider state. Many AP items are now paid down. |
| `docs/architecture/PROVIDER_SPEC_GAPS.md` | Updated | Moabits section now reflects implemented v2 listing enrichment and the remaining mapper gaps. |
| `docs/architecture/MOABITS_ORION_GATEWAY_API_V2.md` | Updated | Documents v2 contract plus backend mapping gaps. |
| `docs/architecture/_phase7_consistency_check.md` | Historical | Added explicit historical note. Useful as an architecture consistency artifact, but predates later implementation details such as ADR-010/011. |
| `docs/architecture/_context_state.json` | Historical generator state | Contains phase-era assumptions and should not be treated as live source of truth. |
| `docs/architecture/c4-context.mermaid` | Updated | Postgres and Moabits labels now mention current tables/v1+v2 auth. |
| `docs/architecture/c4-container.mermaid` | Updated | Removed claims of generic cache/retry as implemented behavior; added provider source config/lifecycle audit. |
| `docs/architecture/c4-component.mermaid` | Updated | Reframed domain services as conceptual/router-level because there are no concrete service classes yet; Moabits label mentions v2 enrichment. |
| `docs/architecture/context-map.mermaid` | Updated | Tenancy/Provider labels include provider source config and Moabits v1+v2. |
| `docs/architecture/adrs/ADR-001-*` | Mostly current | Modular monolith is implemented. Generic semaphore mitigation remains target, not implemented. |
| `docs/architecture/adrs/ADR-002-*` | Current | Proxy/routing-map decision matches code. |
| `docs/architecture/adrs/ADR-003-*` | Updated | Removed inaccurate `with_credentials` wrapper example; credentials are passed per call. |
| `docs/architecture/adrs/ADR-004-*` | Current enough | DomainError → problem+json is implemented. |
| `docs/architecture/adrs/ADR-005-*` | Updated | Implementation table reflects breaker done, retry/cache/generic bulkhead/metrics not done. |
| `docs/architecture/adrs/ADR-006-*` | Updated | Corrected Moabits `company_codes`, credential cache, credential audit/idempotency claims. |
| `docs/architecture/adrs/ADR-007-*` | Mostly current | `/v1`, cursor/list response, and idempotency for SIM writes match code. |
| `docs/architecture/adrs/ADR-008-*` | Mostly current | RBAC and lifecycle audit align. Generic audit table exists but generic audit middleware remains incomplete. |
| `docs/architecture/adrs/ADR-009-*` | Mostly current | Tests exist and are extensive; coverage gates/import-linter are still process goals. |
| `docs/architecture/adrs/ADR-010-*` | Current | Matches provider source config implementation. |
| `docs/architecture/adrs/ADR-011-*` | Current | Captures Moabits v2 enrichment and known gaps. |
| `docs/CIRCUIT_BREAKER_IMPLEMENTATION.md` | Updated | Fixed date and status framing. Still intentionally a focused implementation note. |
| `docs/LIFECYCLE_FLAG.md` | Current | Matches `LIFECYCLE_WRITES_ENABLED` behavior. |
| `IMPLEMENTATION_PLAN.md` | Updated | PR-16 added as implemented; still a living roadmap with remaining PRs. |
| `migrations/README.md` | Current | Lists migrations through `006_provider_source_configs.sql`. |
| `tests/README.md` | Updated | Rewritten to match the current test tree and `pytest-httpx` / `pytest-asyncio` setup. |
| `VALIDATION_REPORT.md` | Updated | Replaced stale 2026-04-29 review text with a concise current validation snapshot and remaining gaps. |
| `INTEGRATION_REVIEW.md`, `moabits.md` | Reference / historical | Added explicit reference notes. Useful provider research; should not be read as live implementation status. |

## Highest-Signal Missing Documentation Work

1. Add a maintained endpoint matrix with exact path, role,
   idempotency requirement, feature flag, and backing adapter behavior.
2. Split `ARCHITECTURE.md` into "current implementation" and "target
   roadmap" sections so future readers do not confuse planned NFRs with
   shipped features.
3. Add an operations doc for Moabits onboarding:
   credential PATCH → discover child companies → admin PUT company-codes
   → provider-scoped listing → optional v2 flag smoke test.
4. Add an observability status doc: request IDs and structlog are live;
   metrics/tracing/rate limiting are pending.
5. Decide whether v2 enrichment failures should be reflected in
   `SimListOut.partial` / `failed_providers` or remain per-SIM
   `provider_fields.enrichment_status`.
