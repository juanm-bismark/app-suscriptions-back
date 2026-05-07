# Moabits Orion Gateway API v2

Source: `https://apiv2.myorion.co/v3/api-docs`

This document captures the Moabits second API contract needed by this backend. It intentionally covers only read operations plus the supported SIM write operations: activate, suspend, and purge.

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

Backend integration status: `GET /v1/sims?provider=moabits` uses v1
`/api/company/simList/{companyCode}` for listing and uses v2 only as
optional enrichment of the paginated ICCIDs. See
[ADR-011](adrs/ADR-011-moabits-v2-list-enrichment.md).

## Supported Endpoints

| Method | Path | Operation | Purpose |
|--------|------|-----------|---------|
| `GET` | `/api/v2/sim/{iccidList}` | `getSimDetails` | SIM details for a comma-separated ICCID list |
| `GET` | `/api/v2/sim/service-status/{iccidList}` | `getServiceStatus` | Current service status for a comma-separated ICCID list |
| `GET` | `/api/v2/sim/connectivity/{iccidList}` | `getConnectivityStatus` | Connectivity status for a comma-separated ICCID list |
| `GET` | `/api/v2/product/product-list/{id}` | `getAssignableProducts` | Products assignable to a client |
| `GET` | `/api/v2/client/children` | `getChildren` | Child clients for the authenticated parent client |
| `PUT` | `/api/v2/sim/active` | `activateSims` | Activate SIMs and optionally enable data/SMS services |
| `PUT` | `/api/v2/sim/suspend` | `suspendSims` | Suspend SIMs and optionally disable data/SMS services |
| `PUT` | `/api/v2/sim/purge` | `purgeSims` | Cancel current network location registration and force re-attach |

## Read Operations

### GET `/api/v2/sim/{iccidList}`

Returns SIM detail records for one or more ICCIDs.

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

## Write Operations

All documented write operations use `application/json` request bodies and API key auth through `X-API-KEY`.

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

- v2 detail and connectivity are called with the same `x_api_key` stored
  for v1. No separate `x_api_key_v2` is modeled yet.
- v2 base URL is configured through `MOABITS_V2_BASE_URL`, not per
  tenant credentials.
- `smsLimitMo` and `smsLimitMt` are documented by v2 but are not yet
  preserved separately by the adapter; current normalized limits still
  rely on legacy `smsLimit` when present.
- Connectivity fields `mcc`, `mnc`, `chargeTowards`, `dataSessionId`,
  `dateOpened` and `usageKB` are preserved in `provider_fields` as
  `mcc`, `mnc`, `charge_towards`, `data_session_id`,
  `session_started_at` and `usage_kb`; they are not yet promoted into
  `normalized.network` or `normalized.usage`.
- v2 enrichment degradation is represented per SIM through
  `provider_fields.enrichment_status` (`full`, `detail_only`,
  `connectivity_only`, `v1_only`) and `detail_enriched`, not through
  top-level `SimListOut.partial`.
