# Provider Specification Gaps & Unsupported Features

**Status**: 2026-05-07 | Informational reference document for implemented provider contract details and unimplemented provider capabilities.

## Overview

The three provider adapters implement a **canonical `SubscriptionProvider` interface** that covers core operations: subscription lookup, usage metrics, connectivity presence, and control operations such as `purge`. The public FastAPI surface intentionally stays provider-neutral; provider-specific verbs such as Kite `networkReset`, Tele2 `Edit Device Details {status: PURGED}`, and Moabits purge routes remain inside adapters.

This document tracks:

1. **Missing endpoints** — API features exposed by the provider but not yet implemented
2. **Unimplemented capabilities** — Optional Capability Protocols not yet adopted
3. **Provider-specific limitations** — Operations that a provider does not support

---

## Kite

### Missing Endpoints

| Endpoint | Purpose | Impact | Priority |
|----------|---------|--------|----------|
| Reports API (`createDownloadReport`, `getDownloadReportList`, `getDownloadReportLink`) | Bulk export / reconciliation | Full-account reconciliation still relies on live pagination rather than asynchronous report export | Low |
| Location detail (`getLocationDetail`) | Manual/automatic location history | Location UI not supported in v1 | Low |

### Unsupported Operations

| Operation | Reason | Workaround |
|-----------|--------|-----------|
| `set_administrative_status` outside documented subset | Kite `modifySubscription` only documents `INACTIVE_NEW`, `TEST`, `ACTIVATION_READY`, `ACTIVATION_PENDANT`, and `ACTIVE` as API-settable lifecycle targets | Adapter rejects unsupported targets; use Kite portal/operator workflow for suspended/deactivated/retired flows |
| Data/SMS service selective control | Not applicable to Kite's model | N/A |

### Provider Limitations (Native API)

- Kite API identity is certificate-based in the binding evidence. The backend supports cert-only PFX credentials and emits WS-Security UsernameToken only when a deployment configures both `username` and `password`.
- No direct SMS/data toggle per service type — status control is coarse-grained (ACTIVE/TEST/SUSPENDED/DEACTIVATED)
- Network reset is technical (`networkReset`) and does not change `lifeCycleStatus`; this is intentionally mapped to the backend's canonical `purge()` operation for Kite because the product treats it as the same frontend action class.

---

## Tele2

### Implemented Cisco/Jasper Contract Details

- Base host defaults to `https://restapi3.jasper.com`; callers may override with `credentials.cobrand_url` only when a different Jasper host is contractually assigned.
- API version is fixed in code as `/rws/api/v1`. Client-submitted `api_version` is not persisted by the credential API. Direct adapter calls with an unsupported version fail with `10000024 Invalid apiVersion`.
- Search Devices requires `modifiedSince`. Backend public calls must provide `GET /v1/sims?provider=tele2&modified_since=yyyy-MM-ddTHH:mm:ssZ`; missing values return Cisco-style `{"errorMessage":"ModifiedSince is required.","errorCode":"10000003"}`.
- `modifiedSince` is strict `yyyy-MM-ddTHH:mm:ssZ`, cannot be in the future, and cannot be older than one year.
- `pageSize` defaults to 50 and is clamped to Tele2's maximum of 50. `pageNumber` defaults to 1.
- `modifiedTill` defaults to one year after `modifiedSince` when omitted.
- After Search Devices returns a page, the adapter enriches only the first 5 devices with `Get Device Details` (`GET /rws/api/v1/devices/{iccid}`) before responding. This keeps the response useful for the first visible rows while respecting common Tele2/Cisco fair-use limits such as 5 TPS.
- Enriched rows include `detail_level=detail`; non-enriched Search Devices rows include `detail_level=summary`. A `null` canonical field in a summary row means "not present in this listing response", not "the provider has no value".
- `Get Device Details` fields are mapped to canonical/top-level or normalized response blocks when present: `imsi`, `msisdn`, `imei`, `dateActivated`, `dateUpdated`, `accountId`, fixed IP fields, `deviceID`, `modemID`, `eid`, `euiccid`, `simProfileId`, `simNotes`, `mec`, and custom fields.
- Cisco fair-use throttling is implemented in-process per Tele2 account key: calls are serialized and rate-limited by `account_scope.max_tps` / credential `max_tps`, defaulting to 1 TPS. Advantage accounts can use `max_tps: 5`.
- Cisco rate-limit response `errorCode=40000029` and HTTP 429 both map to `ProviderRateLimited`; the Tele2 limiter increases temporary backoff after rate-limit responses.

Example credential metadata for an Advantage account:

```json
{
  "credentials": {
    "username": "api-user",
    "api_key": "secret"
  },
  "account_scope": {
    "environment": "production",
    "account_type": "advantage",
    "max_tps": 5
  }
}
```

### Not Yet Applied From Cisco Fair-Use Guidance

- Tele2 throttling is **in-process only**. Multiple Uvicorn workers or multiple containers do not share the same TPS budget yet; use a single worker for strict compliance or move the limiter to Redis before horizontal scaling.
- No distributed dynamic TPS allocator exists for accounts with autoscaled SBCTPS, purchased Incremental TPS, or Overage TPS. `max_tps` is manually configured in `account_scope`.
- No provider usage dashboard integration is implemented; Cisco API Usage Dashboard remains the source of truth for real account TPS.
- No long-lived Tele2 cache is implemented. The backend still acts primarily as a live proxy; repeated UI calls can still consume TPS unless the frontend or a future backend cache suppresses them.
- No scheduler enforces "usage only every 6 hours / once per day" business cadence. Call frequency policy must be handled by clients or future jobs.

### Missing Endpoints

| Endpoint | Purpose | Impact | Priority |
|----------|---------|--------|----------|
| Get Aggregated Usage Details | Retrieve usage aggregated by carrier, country, zone, or plan | No cross-SIM analytics in UI | Low |
| Get Service Type Details / List Plans | Enumerate available communication plans and service changes | Plan change UI not supported in v1 | Low |

### Consolidation Note

Both `set_administrative_status(target=AdministrativeStatus.PURGED)` and `purge()` reach the same provider state by issuing:
```
PUT /rws/api/v1/devices/{iccid} {"status": "PURGED"}
```
The `purge()` method delegates to `set_administrative_status()` to avoid duplication.

### Unsupported Operations

- Selective data/SMS service control — Tele2 controls status at the device level, not per service. `set_administrative_status()` ignores `data_service` and `sms_service` flags.
- Network reset as a distinct provider operation — the REST catalog has no endpoint separate from `Edit Device Details`.

### Provider Limitations (Native API)

- No separate SMS/data service toggle — status transitions affect the entire device
- No device reactivation from PURGED state (permanent)

---

## Moabits

Moabits has two separate Orion API surfaces. The adapter uses the older Orion v1 API for authorization, company discovery, per-SIM detail, usage, presence and lifecycle writes. For provider-scoped listing, it uses v1 `simList` as the source of ICCIDs and can optionally enrich that page with Orion Gateway API v2 detail/connectivity behind `MOABITS_V2_ENRICHMENT_ENABLED` (ADR-011).

The dedicated v2 reference for this backend is [MOABITS_ORION_GATEWAY_API_V2.md](MOABITS_ORION_GATEWAY_API_V2.md). It intentionally documents only the needed read endpoints plus `active`, `suspend`, and `purge`.

Confirmed API v2 paths in scope:
- `GET /api/v2/sim/{iccidList}`
- `GET /api/v2/sim/connectivity/{iccidList}`

Documented by Swagger but not used by current backend code:
- `GET /api/v2/sim/service-status/{iccidList}`
- `GET /api/v2/product/product-list/{id}`
- `GET /api/v2/client/children`
- `PUT /api/v2/sim/active`
- `PUT /api/v2/sim/suspend`
- `PUT /api/v2/sim/purge`

Current backend gaps in the v2 enrichment mapper:
- `smsLimitMo` and `smsLimitMt` are documented by v2 but are not preserved separately yet.
- v2 connectivity fields such as `mcc`, `mnc`, `dataSessionId`, `dateOpened`, `chargeTowards` and `usageKB` remain in `provider_fields`; they are not promoted into `normalized`.
- v2 enrichment failures are per-SIM (`provider_fields.enrichment_status`) and logs; they do not set top-level `SimListOut.partial`.

### Out of Scope for the Current Moabits v2 Reference

| Endpoint | Reason |
|----------|--------|
| `PUT /api/v2/sim/{iccid}/name` | SIM rename is not needed for this backend scope. |
| `PUT /api/v2/sim/limits` | Quota writes are not needed for this backend scope. |
| `POST /api/v2/product/assignSIM` | Product assignment is not needed for this backend scope. |

### Provider Limitations (Native API v2)

- API v2 declares API key auth through header `X-API-KEY`; no JWT bootstrap endpoint is documented in this Swagger.
- Bulk SIM operations do not document maximum item counts or partial-success response schemas.
- Several success responses are declared as generic `object`, so the success payload contract is incomplete.
- Dates are plain strings without declared `date-time` format or timezone semantics.
- `GET /api/v2/product/product-list/{id}` and `GET /api/v2/client/children` have schema/example mismatches in Swagger.

---

## Future Capability Protocols

As the product evolves, optional capabilities can be introduced as separate `Protocol` definitions (see [ADR-003](adrs/ADR-003-acl-provider-adapter.md)):

- **`HistoryProvider`** — `async get_status_history(iccid, credentials, *, limit, offset) -> list[StatusChange]` (Kite only)
- **`PlanManagementProvider`** — `async list_plans(credentials) -> list[Plan]`, `async change_plan(iccid, credentials, *, plan_id)` (Tele2 only)
- **`QuotaManagementProvider`** — `async set_sim_limits(iccid, credentials, *, data_limit_mb, sms_limit)` (Moabits only)

These can be adopted incrementally:
1. Add the `Protocol` interface in `app/providers/base.py`
2. Implement in adapter(s) that support it
3. Update router/service to dispatch via `isinstance(adapter, CapabilityProtocol)`
4. Add Pydantic schemas and OpenAPI docs for the new endpoints

---

## Product Decisions Reflected in Gaps

| Gap | Reason |
|-----|--------|
| Kite lifecycle changes outside documented subset not exposed | Kite `modifySubscription` documents only `INACTIVE_NEW`, `TEST`, `ACTIVATION_READY`, `ACTIVATION_PENDANT`, and `ACTIVE` as settable targets. The adapter rejects unsupported targets. |
| Tele2/Moabits aggregated usage not exposed | These are rollup features; core v1 requirement is per-SIM metrics. Can be added in v2 with separate endpoints. |
| Plan changes not implemented | Requires detailed plan catalog & change validation. Out of scope for v1 (read-only on status & usage). |
| Moabits SIM limit writes not exposed | Orion exposes `PUT /api/sim/setLimits/`, but quota writes are not part of backend v1. |
| Provider-specific purge/network reset endpoints not exposed | Architecture uses one canonical `POST /v1/sims/{iccid}/purge` so the frontend does not branch by provider. Adapters map to native verbs. |

---

## Next Steps

1. **v2 roadmap**: Prioritize Capability Protocols based on product requirements
2. **Kite status history**: If required, expose through an optional capability; Tele2/Moabits should return `not_supported`
3. **Usage analytics**: Add aggregation layer if cross-tenant reporting is needed
4. **SIM management**: Consider `updateSimName` + limits as part of advanced SIM lifecycle feature set
