# Moabits Orion Gateway API v2

Source: `https://apiv2.myorion.co/swagger-ui/index.html#/` backed by
`https://apiv2.myorion.co/v3/api-docs`.

This document captures the Moabits second API contract needed by this backend.
Current product scope uses v2 for SIM read enrichment only: `GET
/api/v2/sim/{iccidList}` and connectivity. There is no separate v2 `/details`
route; the `getSimDetails` operation is the SIM read endpoint itself. The
canonical public mutation remains purge; v2 activate/suspend operations are
documented by Moabits but are outside the current SIM-view scope.

## Metadata

| Field | Value |
|-------|-------|
| Title | Orion Gateway API |
| Version | 1.0.2 |
| OpenAPI | 3.0.1 |
| Production server | `https://apiv2.myorion.co` |
| Contact | Orion Support Team, `support@moabits.com` |
| License | Proprietary, `https://moabits.com/` |
| Auth | API key in header `X-API-KEY` |

The v2 API declares a single security scheme: `apiKey` in the `X-API-KEY` header. Unlike the older Moabits API currently reflected in some code/docs, this API does not document a JWT bootstrap endpoint.

Backend integration status: `GET /v1/sims?provider=moabits` still uses the
confirmed v1 `/api/company/simList/{companyCode}` listing and, by default,
attempts v2 SIM/connectivity enrichment for the ICCIDs in the returned page.
Single SIM lookup also prefers v2 `GET /api/v2/sim/{iccidList}` and falls back to legacy v1 `GET
/api/sim/details/{iccidList}` only when v2 has no usable data. See
[ADR-011](adrs/ADR-011-moabits-v2-list-enrichment.md). Current v2 Swagger also
documents `GET /api/v2/company/sim-list/{companyCodes}` and `GET
/api/v2/company/sim-list-detail/{companyCodes}`; those are not integrated yet.

## Backend Listing Flow

The backend currently keeps v1 as the source of the SIM universe. The v2 Swagger
now documents company listing endpoints, but the integration has not switched to
them yet because the established production path and field mapping are v1 list
plus v2 enrichment. The backend listing flow is:

1. Call v1 `GET /api/company/simList/{companyCode}` to discover ICCIDs plus
   `simStatus`, `dataService`, and `smsService`.
2. Page locally over those v1 rows.
3. For the ICCIDs in the requested page, call v2 in batches:
   - `GET /api/v2/sim/{iccidList}` for the SIM record and its detail fields.
   - `GET /api/v2/sim/connectivity/{iccidList}` for live connectivity data.
4. Merge v1 and v2 into one canonical `SubscriptionOut`.

This enrichment is enabled by default with `MOABITS_V2_ENRICHMENT_ENABLED=true`.
Setting it to `false` keeps the legacy v1-only listing behavior. v2 failures are
degradable: a failed SIM/connectivity batch logs
`moabits_v2_enrichment_chunk_failed` and the endpoint still returns the v1 rows.
The v2 calls are batched and cached briefly per ICCID to avoid repeating the
same SIM/connectivity lookups during rapid UI refreshes.

Common fields go under `normalized`; provider-specific or diagnostic fields stay
under `provider_fields`. Notable Moabits mappings:

| Source | Output |
|--------|--------|
| v1 `simStatus` | top-level `status` |
| v1 `dataService`, `smsService` | `provider_fields.data_service`, `provider_fields.sms_service`, `provider_fields.services` |
| v2 SIM read `imsiNumber` | top-level `imsi` and `provider_fields.imsi_number` |
| v2 SIM read `imsi` | `provider_fields.imsi_raw` |
| v2 SIM read `dataLimit` | `provider_fields.data_limit_mb` as integer |
| v2 SIM read `smsLimitMo`, `smsLimitMt` | `provider_fields.sms_limit_mo`, `provider_fields.sms_limit_mt`; summed into `sms_limit` when no total is provided |
| v2 connectivity `network`, `country`, `rat`, `privateIp` | `provider_fields.operator`, `country`, `rat_type`, `ip_address` |
| v2 connectivity `mcc`, `mnc`, `dataSessionId`, `dateOpened`, `chargeTowards`, `usageKB`, `imsi` | `provider_fields.mcc`, `mnc`, `data_session_id`, `session_started_at`, `charge_towards`, `usage_kb`, `connectivity_imsi_raw` |

## Supported Endpoints

| Method | Path | Operation | Purpose |
|--------|------|-----------|---------|
| `GET` | `/api/v2/sim/{iccidList}` | `getSimDetails` | SIM record, including detail fields, for a comma-separated ICCID list |
| `GET` | `/api/v2/sim/service-status/{iccidList}` | `getServiceStatus` | Current service status for a comma-separated ICCID list |
| `GET` | `/api/v2/sim/connectivity/{iccidList}` | `getConnectivityStatus` | Connectivity status for a comma-separated ICCID list |
| `GET` | `/api/v2/product/product-list/{id}` | `getAssignableProducts` | Products assignable to a client |
| `GET` | `/api/v2/client/children` | `getChildren` | Child clients for the authenticated parent client |
| `GET` | `/api/v2/company/sim-list/{companyCodes}` | `getSimList` | Documented by Moabits; not integrated yet |
| `GET` | `/api/v2/company/sim-list-detail/{companyCodes}` | `getSimListDetail` | Documented by Moabits; not integrated yet |
| `GET` | `/api/v2/company/children/{companyCode}` | `getCompanyChildren` | Documented by Moabits; not integrated yet |
| `GET` | `/api/v2/flex-plan/sim/{iccid}/status` | `getFlexPlanStatus` | Documented by Moabits; candidate SIM read, not integrated yet |
| `PUT` | `/api/v2/sim/active` | `activateSims` | Documented by Moabits; outside current backend scope |
| `PUT` | `/api/v2/sim/suspend` | `suspendSims` | Documented by Moabits; outside current backend scope |
| `PUT` | `/api/v2/sim/purge` | `purgeSims` | Documented by Moabits; backend canonical purge currently uses the v1 Orion route |

## Read Operations

### GET `/api/v2/sim/{iccidList}`

Returns SIM records, including their detail fields, for one or more ICCIDs.

Path parameters:

| Name | Type | Required | Notes |
|------|------|----------|-------|
| `iccidList` | string | yes | Comma-separated ICCIDs. `minLength: 1`, `maxLength: 4000`. Example: `8910300000000123456,8910300000000123457` |

Documented success schema: `SimDetailsResponseDTO`

Important fields under `info.simInfo[]`:

| Field | Type | Notes |
|-------|------|-------|
| `iccid` | string | Canonical SIM identifier |
| `msisdn`, `imsi`, `imsiNumber`, `imei` | string | SIM identifiers |
| `lastNetwork` | string | Last network observed |
| `first_lu`, `first_cdr`, `last_lu`, `last_cdr`, `firstcdrmonth` | string | Dates are plain strings; no date-time format is declared |
| `product_id` | integer | `int32` in Swagger |
| `product_name`, `product_code` | string | Current product/plan labels |
| `clientName`, `companyCode` | string | Customer/client fields |
| `dataLimit`, `smsLimitMo`, `smsLimitMt` | string | Limits are strings in this read schema |
| `services` | string | Provider service string |
| `numberOfRenewalsPlan`, `remainingRenewalsPlan` | integer | `int32` |
| `planStartDate`, `planExpirationDate`, `statusPlan` | string | Plain strings; no date-time format is declared |
| `nextChangeMonth`, `nextChangeYear` | integer | `int32` |
| `nextProductName`, `nextProductCode` | string | Future product metadata |
| `parametrizedAutorenewal` | integer | `int64` |
| `statusAutorenewal` | string | Autorenewal status |

Documented errors: `400 Invalid request`, `404 No SIMs found for given list`, `500 Server error`.

Notes:

- Swagger declares content type `*/*` instead of `application/json`.
- Error responses reuse `SimDetailsResponseDTO`, which is inconsistent with the other endpoints.
- No pagination is documented; the path length limit is the practical bulk limit.

### GET `/api/v2/sim/service-status/{iccidList}`

Returns current service status for one or more ICCIDs.

Path parameters:

| Name | Type | Required | Notes |
|------|------|----------|-------|
| `iccidList` | string | yes | Comma-separated ICCIDs. `minLength: 1`, `maxLength: 4000` |

Documented success schema: generic `object`.

Documented errors: `400 Invalid request`, `401 Unauthorized - invalid or missing API key`, `403 Forbidden - insufficient permissions to view service status`.

Notes:

- The real response fields are not described by Swagger.
- No `404` response is documented.

### GET `/api/v2/sim/connectivity/{iccidList}`

Returns real-time connectivity status for one or more ICCIDs.

Path parameters:

| Name | Type | Required | Notes |
|------|------|----------|-------|
| `iccidList` | string | yes | Comma-separated ICCIDs. `minLength: 1`, `maxLength: 4000` |

Documented success schema: `ConnectivityStatusDTO`

| Field | Type | Notes |
|-------|------|-------|
| `iccid` | string | SIM identifier |
| `dataSessionId` | string | Data session identifier |
| `dateOpened` | string | Plain string; no date-time format is declared |
| `mcc`, `mnc` | string | Strings preserve leading zeroes |
| `imsi` | string | IMSI |
| `usageKB` | number | `double` |
| `rat` | string | Radio access technology |
| `privateIp` | string | Private IP address |
| `chargeTowards` | string | Charging direction/target |
| `country`, `network` | string | Current network location |

Documented errors: `400 Invalid request`, `401 Unauthorized - invalid or missing API key`, `403 Forbidden - insufficient permissions to view connectivity status`.

Notes:

- The path accepts an ICCID list, but the documented response is a single object, not an array.
- No `404` response is documented.

### GET `/api/v2/product/product-list/{id}`

Returns products assignable to the provided client id.

Path parameters:

| Name | Type | Required | Notes |
|------|------|----------|-------|
| `id` | integer | yes | `int64`; no Swagger description |

Documented success schema: array of `ProductDTO`.

| Field | Type | Notes |
|-------|------|-------|
| `name` | string | Product name |
| `prodid` | integer | `int64`; Swagger example uses `productId` instead |

Documented errors: `401 Unauthorized - invalid or missing API key / bearer`, `403 Forbidden - insufficient permissions`.

Notes:

- Swagger schema says `prodid`, but the example says `productId`.
- The `401` text mentions bearer auth even though only `X-API-KEY` is declared.
- No `400` or `404` response is documented.

### GET `/api/v2/client/children`

Returns child clients associated with the authenticated parent client.

Parameters: none.

Documented schema: `CompanyDTO`, but the example is an array of company records.

| Field | Type | Notes |
|-------|------|-------|
| `companyCode` | string | Child company code |
| `companyName` | string | Child company name |
| `clieId` | integer | `int64`; included in schema/example though omitted from the description |

Documented errors: `401 Unauthorized - invalid or missing API key`, `403 Forbidden - insufficient permissions`.

Notes:

- The schema should likely be `CompanyDTO[]`, not a single `CompanyDTO`.
- No pagination or filters are documented.

### Other GETs Documented By Swagger

The v2 Swagger also exposes read endpoints that are not wired into the backend:

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/v2/company/sim-list/{companyCodes}` | Potential future replacement for v1 `simList`; requires real-account validation before switching the source of the SIM universe. |
| `GET` | `/api/v2/company/sim-list-detail/{companyCodes}` | Potential future replacement for v1 list + v2 SIM enrichment; requires parity checks for status/services and payload shape. |
| `GET` | `/api/v2/company/children/{companyCode}` | Company hierarchy/admin read; outside SIM detail view. |
| `GET` | `/api/v2/flex-plan/sim/{iccid}/status` | SIM-scoped GET and possible future provider-specific read; not currently mapped into `SubscriptionOut`. |
| `GET` | `/api/v2/lookup/countries`, `/api/v2/lookup/currencies`, `/api/v2/lookup/payment-methods`, `/api/v2/lookup/roles` | Lookup/admin data, outside SIM detail view. |

## Write Operations Documented By Moabits

All documented write operations use `application/json` request bodies and API key auth through `X-API-KEY`.

Backend note: these v2 write operations are not part of the current integration.
The application exposes purge as the only canonical control operation for this
provider scope, and the existing implementation maps it to the confirmed v1
Orion purge endpoint.

Swagger also documents read-like POST endpoints (`POST /api/v2/usage/by-iccids`
and `POST /api/v2/usage/by-company`). They remain outside the current Moabits
SIM-view scope because this integration only adds provider reads that are GETs,
with purge as the sole canonical control exception.

### PUT `/api/v2/sim/active`

Activates one or more SIMs by ICCID and optionally enables data and/or SMS services.

Request body: `SimActivateRequest`

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `iccidList` | string[] | yes | ICCIDs to activate |
| `dataService` | boolean | no | Whether to enable data service |
| `smsService` | boolean | no | Whether to enable SMS service |

Documented success schema: generic `object`.

Documented errors: `400 Invalid request`, `401 Unauthorized - invalid or missing API key`, `403 Forbidden - insufficient permissions`.

Notes:

- No maximum number of ICCIDs is documented.
- Swagger does not define what happens when both service flags are false or omitted.
- No partial-success schema is documented.

### PUT `/api/v2/sim/suspend`

Suspends one or more SIMs by ICCID and optionally disables data and/or SMS services.

Request body: `SimSuspendRequest`

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `iccidList` | string[] | yes | ICCIDs to suspend |
| `dataService` | boolean | no | Whether to disable data service |
| `smsService` | boolean | no | Whether to disable SMS service |

Documented success schema: generic `object`.

Documented errors: `400 Invalid request`, `401 Unauthorized - invalid or missing API key`, `403 Forbidden - insufficient permissions`.

Notes:

- `SimSuspendRequest` has the same shape as `SimActivateRequest`.
- No maximum number of ICCIDs is documented.
- No partial-success schema is documented.

### PUT `/api/v2/sim/purge`

Purges one or more SIMs by cancelling current network location registration and forcing re-attachment.

Request body: `SimPurgeRequest`

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `iccidList` | string[] | yes | ICCIDs to purge |

Documented success schema: generic `object`.

Documented errors: `400 Invalid request`, `401 Unauthorized - invalid or missing API key`, `403 Forbidden - insufficient permissions`.

Notes:

- Swagger does not document idempotency behavior.
- No maximum number of ICCIDs is documented.
- No granular success/failure schema is documented.

## Contract Risks

- Several response schemas are underdocumented as generic `object`.
- Bulk operations accept ICCID arrays or comma-separated lists but do not document maximum item counts.
- The API does not document partial success for multi-ICCID operations.
- Dates are plain strings without declared `date-time` format or timezone semantics.
- Product and client examples conflict with their declared schemas.
- Product error messages mention bearer auth, but the declared security model only includes `X-API-KEY`.

## Backend Mapping Notes

Current implementation details compared with this contract:

- v2 SIM read and connectivity are called with the same `x_api_key` stored
  for v1. No separate `x_api_key_v2` is modeled yet.
- v2 base URL is configured through `MOABITS_V2_BASE_URL`, not per
  tenant credentials.
- `smsLimitMo` and `smsLimitMt` are preserved separately by the adapter as
  `provider_fields.sms_limit_mo` and `provider_fields.sms_limit_mt`; when no
  total `smsLimit` is provided, `sms_limit` is computed from their sum.
- Connectivity fields `mcc`, `mnc`, `chargeTowards`, `dataSessionId`,
  `dateOpened` and `usageKB` are preserved in `provider_fields` as
  `mcc`, `mnc`, `charge_towards`, `data_session_id`,
  `session_started_at` and `usage_kb`; they are not yet promoted into
  `normalized.network` or `normalized.usage`.
- v2 enrichment degradation is represented per SIM through
  `provider_fields.enrichment_status` (`full`, `detail_only`,
  `connectivity_only`, `v1_only`) and `detail_enriched`, not through
  top-level `SimListOut.partial`.
