# Multi-Provider IoT/M2M Integration Review

**Status:** Updated after provider-adapter remediation, with documented evidence from both vendor notebooks.
**Date:** 2026-05-05
**Scope:** Kite (Telefónica UNICA), Tele2 (Cisco Control Center / Jasper), Moabits (Orion API 2.0)
**Author:** Senior Solution Architect / Backend Lead / API Documentation Auditor review

---

## 0. Sources & evidence

| Provider | Source | Status |
|---|---|---|
| Kite | "Kite Platform UNICA API SOAP Binding Specification" (NotebookLM `becadcf2…`) + local WSDLs in `app/providers/kite/wsdl/` | **Consulted.** WSDL is the contract; the binding spec adds enums, glossary, error table, search params, and request/response examples. |
| Tele2 | "Tele2 Cisco Control Center REST API Resource Catalog" (NotebookLM `83cdd80c…`, 22 sources, mirrored from `tele2.jasperwireless.com`) | **Consulted.** Authoritative for every Tele2 row below. |
| Moabits | `moabits.md` (Spanish narrative summary, sections 1–6) + Orion API 2.0.0 Swagger (`https://www.api.myorion.co/api-doc`) | Consulted for auth, server URL, core paths, purge body/response, and writeable transitions. Field payload samples still needed for casing/shape validation. |

**Evidence labels:**
- **(D)** Documented in the vendor source.
- **(O)** Observed in adapter code only — not yet verified against vendor docs.
- **(I)** Reasonable inference.
- **(NC)** Not confirmed.
- **(NS)** Not supported.

---

## 1. Per-endpoint review — Kite (UNICA SOAP, Inventory v12)

Service URL (D-WSDL, `inventory_services_v12_0.wsdl:441`):
`https://kiteplatform-api.telefonica.com:8010/services/SOAP/GlobalM2M/Inventory/v12/r12`

**Authentication:** the binding spec evidence ties API access to the consumer public SSL certificate and ADMIN role permissions. NotebookLM review found no `UsernameToken` in the SOAP examples and no username/password authentication parameter for API consumption. The adapter now supports certificate-only mutual TLS from an encrypted base64 PFX credential (`client_cert_pfx_b64`, optional `client_cert_password`), and emits WS-Security UsernameToken only when both `username` and `password` are configured for a deployment that explicitly requires it. [REQUIRES INPUT: confirm whether this specific production tenant is cert-only or requires legacy WSSE in addition to mTLS].

### 1.1 `getSubscriptions`

- **Capability:** `list_lines`
- **SOAP Action:** `urn:getSubscriptions`
- **Input parameters:**
  - `maxBatchSize: xsd:int` — **D max = 1000** (per fault example: `"Supported values are integers minor or equal than 1000 and bigger than 0"`).
  - `startIndex: xsd:int` — offset.
  - `searchParameters: SearchParamsType` — list of `(name, value)` filters.
- **Documented `searchParameters` names (D):**
  - Identifiers: `icc, imsi, msisdn, imei, eid, alias`.
  - Custom: `customField_1..4`.
  - **Lifecycle filters: `lifeCycleState`** (note the doc spells this `lifeCycleState`, not `lifeCycleStatus`).
  - **Status-change date filters: `startLastStateChangeDate, endLastStateChangeDate, lastStateChangeDate, startLastCommercialGroupChangeDate, endLastCommercialGroupChangeDate, suspensionNextDate`**. These ARE the incremental-sync hooks. (D)
  - Provisioning dates: `provisionDate, shippingDate, activationDate`.
  - Network: `ip, apn, staticIP, enabledApn, ggsnIP, sgsnIP, subnet, subnetMask, presence, ratType` (with documented `ratType` values 1=3G, 2=2G, 5=3.5G, 6=4G, 8=NB-IoT, 9=LTE-M, 10=5G SA).
  - Geography: `operator, country, postalCode, region`.
  - Behavior: `stalledDays, unusedDays, usedDays, aggressiveBehaviour`.
  - Tech enabled/used: `tec2GEnabled, tec3GEnabled, tecNbIotEnabled, tecLteEnabled, tec5GEnabled, tec2GUsed, tec3GUsed, tec35GUsed, tec4GUsed, tec5GUsed, tecLteUsed, tecNbIotUsed`.
  - Consumption: `startSmsConsumptionDate, endSmsConsumptionDate, startVoiceConsumptionDate, startGprsUpDate, endGprsUpDate`.
  - Hardware: `simModel, imeiLock, imeiLockEnabled`.
  - Swap (eUICC only): `swapStatus, subscriptionType (UICC|EUICC)`.
  - **Presence filter values:** `ip, gprs, !ip, !gprs` (the `!` prefix flips to DOWN). (D)
  - **`endState`** (used together with `startLastStateChangeDate` / `endLastStateChangeDate`) accepts: `INACTIVE_NEW, TEST, ACTIVATION_PENDANT, ACTIVATION_READY, DEACTIVATED, ACTIVE, SUSPENDED, RETIRED, RESTORE`.
- **Identifiers supported:** account-scoped via the Kite credential/certificate; per-row filter by `icc/imsi/msisdn/imei/eid`.
- **Response payload:** `subscriptionData[]` of `SubscriptionInfoType` — full SIM detail including `consumptionDaily`, `consumptionMonthly`, `expenseMonthly`, `gprsStatus`, `ipStatus`, `basicServices`, `supplServices`, custom fields, etc. (D-WSDL)
- **Mutation behavior:** read-only.
- **Pagination:** `startIndex` + `maxBatchSize ∈ [1..1000]`. (D)
- **Errors:** `ClientException` (`SVC, POL, SEC`) and `ServerException` (`SVR`) — full table at the end of this section.
- **Confidence:** **HIGH.**
- **Open questions:** does `searchParameters` accept multiple `(name,value)` pairs as AND filters (the doc shows them as a list, suggesting yes)?

### 1.2 `getSubscriptionDetail`

- **Capability:** `get_line_detail`
- **Input:** xsd:choice `icc | imsi | msisdn | subscriptionId`.
- **Response:** `SubscriptionDetailType` — **a thinner shape than `SubscriptionInfoType`**:
  - It carries identity, dates, plan basics (`commercialGroup, supervisionGroup, billingAccount, apn, staticIp, apn0..9, staticApnIndex`), `customField1..4`, customer/master/serviceProvider hierarchy, and **`lifeCycleStatus`**.
  - **It does NOT carry consumption blocks, GPRS/IP status, basicServices, supplServices, expenseMonthly, locations, country/operator, blockReason, or last*Date fields.** (D-WSDL — see XSD lines 214–275).
- **Implication:** to build a complete `LineDetail` for a single SIM you must compose **`getSubscriptionDetail` + `getSubscriptions(searchParameters[icc=<id>, maxBatchSize=1])` + `getStatusDetail` + `getPresenceDetail`**. The Cisco-style "single fat call" pattern does not exist on Kite.
- **Confidence:** **HIGH.**

### 1.3 `getPresenceDetail`

- **Capability:** `get_presence`
- **Response (D-doc, `PresenceDetailType`):**
  - `level: xsd:string` — values: `unknown, GSM, GPRS, IP reachability`.
  - `timeStamp: xsd:dateTime`.
  - `cause: xsd:string` — example value `UNKNOWN_SUBSCRIBER`. **No full enum is documented.**
  - `ip: xsd:string?` (only when active connection).
  - `apn: xsd:string?` (only when active connection).
  - `ratType: xsd:int?` — refers to 3GPP TS 29.274 §8.17 with the value table above.
- **Recommended online rule:** ONLINE iff `level ∈ {GPRS, "IP reachability"}`. **`level=GSM` should map to `OFFLINE` or a future `voice_only` state — NOT to ONLINE**, because GSM means camped on 2G voice, no data session. Current mapper tests cover `GPRS`, `IP reachability`, `GSM`, and `unknown`.
- **Real-time vs cached:** the doc labels the GPRS/IP status as "Real-time status of GPRS connection" (`GprsStatusType.status`). Presence detail itself only carries a `timeStamp` for last-known state. Treat `level` as **last-known** with the documented `timeStamp` indicating freshness.
- **Confidence:** **HIGH** for shape; **HIGH** for online rule once we accept the glossary.
- **Open questions:** the `cause` enum (only `UNKNOWN_SUBSCRIBER` is given as an example).

### 1.4 `getStatusDetail`

- **Capability:** `get_status`
- **Response (D-doc, `StatusDetailType`):**
  - `state: xsd:string` — **same enum as `lifeCycleStatus`** (D — both are described as "Current administrative status of the Subscription").
  - `automatic: xsd:boolean` — **D meaning: "True if the status change was triggered by the platform (e.g. when test voucher expires or after timeout)."**
  - `changeReason: xsd:string?` — populated only when `automatic=true`. (D)
  - `currentStatusDate: xsd:dateTime`.
  - `user: xsd:string?` — only when an operator triggered the change manually.
- **Confidence:** **HIGH.**

### 1.5 `getStatusHistory`

- **Capability:** `get_status_history`
- **Input:** id choice + optional `startDate`, `endDate` (xsd:dateTime).
- **Response:** `statusHistoryData[]` of `StatusHistoryType{state, automatic, time, reason?, user?}`.
- **Pagination:** none documented; bounded only by date range. [REQUIRES INPUT: server-side cap on number of records returned per call].
- **Confidence:** **HIGH.**

### 1.6 `modifySubscription` — ⚠️ critical findings

- **Capability:** `set_administrative_status` + edit plan/profile/limit/services
- **Input:** id choice + xsd:choice of mutually exclusive blocks (full list in `inventory_types_v12_0.xsd:607-693`).
- **`lifeCycleStatus` change block (D-doc, exact quote):**
  - **"Available values are: `INACTIVE_NEW, TEST, ACTIVATION_READY, ACTIVATION_PENDANT, ACTIVE`."** — **Five and only five target states are documented.**
  - **`SUSPENDED, DEACTIVATED, RETIRED, RESTORE` are NOT documented as valid `modifySubscription` targets.** (D — by absence from the documented list)
  - The doc reinforces this with the glossary: "Suspended: ... it can only return to its previous state. **The customer can't change state to/from Suspended.**" (D, glossary). And: "Retired: ... can only be set for a Subscription that already is in suspended state." (D, glossary)
  - **Conclusion:** lifecycle changes via the API are limited to a constrained subset. To get a SIM to `RETIRED`, a different (probably operator-side or portal) workflow is required to first suspend it. To `DEACTIVATED`, you must use the canonical "Deactivate" path the API exposes — **but no such path is in the inventory v12 WSDL.** [REQUIRES INPUT: is `DEACTIVATED` reachable via API at all, or only via portal?]
- **`forceRetired` flag (in the WSDL XSD):** **NOT documented in the binding spec we have access to.** It exists in the XSD (`inventory_types_v12_0.xsd:619`) but the spec excerpts do not describe it. [REQUIRES INPUT].
- **Response:** empty body — confirmed by the success example in the doc (`<modifySubscriptionResponse/>`).
- **Sync vs async:** the empty success response indicates **synchronous acknowledgement of the request**, but the spec does not state whether the lifecycle change is then applied immediately or queued. (D — no explicit statement, but transition errors surface in the same response, which suggests synchronous validation at minimum).
- **Documented transition errors (example):** Fault `SVC.1021` with text `"Response with an error: 2. Reason: There is not an available transition from SUSPENDED to TEST state."`. **This proves Kite enforces a state machine and will reject invalid transitions with `SVC.1021`.** (D)
- **Other edit blocks (D):**
  - Custom fields: `alias, customField1..4`.
  - Consumption thresholds: `dailyConsumptionThreshold, monthlyConsumptionThreshold, monthlyExpenseLimit` (these ARE the limits-edit path).
  - Basic services: `voiceMOHomeEnabled, voiceMOInternationalEnabled, voiceMORoamingEnabled, voiceMTHomeEnabled, voiceMTRoamingEnabled, smsMOHomeEnabled, smsMOInternationalEnabled, smsMORoamingEnabled, smsMTHomeEnabled, smsMTRoamingEnabled, dataHomeEnabled, dataRoamingEnabled`.
  - Supplementary services: `vpnEnabled, advancedSupervisionEnabled, locationEnabled`.
  - APN/IP: `apn0..9, staticIpAddress0..9, additionalStaticIpAddress0..9, defaultApn`.
  - Network tech: `lteEnabled, qci, voLteEnabled`.
  - Group: `commercialGroup, supervisionGroup`.
- **Idempotency:** no documented mechanism in SOAP. SOATransactionID is logged but not used as an idempotency key.
- **Current adapter status:** `KiteAdapter.set_administrative_status` is implemented against `modifySubscription` for the documented target subset `{INACTIVE_NEW, TEST, ACTIVATION_READY, ACTIVATION_PENDANT, ACTIVE}` only, gated by `LIFECYCLE_WRITES_ENABLED`. The SOAP payload sends `lifeCycleStatus`; `requested_status` exists only as an internal Python argument name.
- **Confidence:** **HIGH** for shape and target subset; **MEDIUM** on async vs sync apply; **LOW** on `forceRetired`.

### 1.7 `getTimeAndConsumption` / `modifyTimeAndConsumption`

- **Voucher-based consumption window**, separate from `consumptionMonthly`. The doc glossary defines it: **"Time and Consumption voucher: setting a Time/Data voucher at the Subscription level. Voucher time in seconds, data in bytes; once voucher time or data reaches zero the Subscription is not allowed to consume additional data until voucher is set again."** (D)
- **Implication:** voucher logic is per-customer-enabled, not the standard usage path. Don't use this for "current usage" — `consumptionMonthly` is the right source.

### 1.8 `getLocationDetail`

- **Capability:** geolocation. Returns `manualLocation` and/or `automaticLocation`. Out of scope today; not implemented in the adapter.

### 1.9 `sendSMS / getSendSMSResult / downloadAndActivateProfile / auditSwapProfile`

- Out of scope for line management; documented but not implemented. `downloadAndActivateProfile` and `auditSwapProfile` apply only to eUICC SIMs.

### 1.10 `networkReset` — confirmed semantics

- **Capability:** `network_reset` (technical).
- **Input (D-doc):** id choice + optional `network2g3g: xsd:boolean` + optional `network4g: xsd:boolean`.
- **Doc title:** **"cancel location of the selected subscription for the radio technologies"**. (D)
- **Effect:** clears network attachment for the chosen radio family — same as a HLR cancel-location. **Does NOT change `lifeCycleStatus`.** (D — the doc never lists this as a lifecycle operation, and the response is empty).
- **Cool-down / rate cap:** **not documented in the spec excerpts.** [REQUIRES INPUT: ask Telefónica — typical M2M platforms cap to ~1/min/SIM].
- **Current product/API decision:** the canonical internal `purge()` operation intentionally maps Kite to `networkReset` while Tele2/Moabits map to their administrative purge-style operations. This keeps one backend control operation, but the adapter docstring explicitly records that Kite does **not** change `lifeCycleStatus`.
- **Confidence:** **HIGH.**

### 1.11 Other Kite services in WSDL (out of line-management scope)

| Service | Operations | Capability |
|---|---|---|
| **End Customer v2** | `createEndCustomer, getEndCustomer, getEndCustomers, modifyEndCustomer, deleteEndCustomer, deactivateEndCustomer, activateEndCustomer` | tenant management |
| **User v3** | `createUser, deleteUser, modifyUser, getUsers, blockUser, unblockUser, getRoles, resetPassword` | `user_management` for portal users |
| **Reports v1.16** | `createDownloadReport, getDownloadReportList, getDownloadReportLink, deleteDownloadReport` | **bulk export — best path for full-account reconciliation** instead of paging `getSubscriptions` |
| **Echo v1** | `echo` | health |

### 1.12 Kite error model (D)

| Category | ID | Text | Used for |
|---|---|---|---|
| SVC | 0002 | Invalid parameter value: %1 | Schema/enum validation failure |
| SVC | 0003 | Invalid parameter value: %1. Possible values are: %2 | Same as 0002 with allowed values |
| SVC | 1000 | Missing mandatory parameter: %1 | |
| SVC | 1001 | Invalid parameter: %1 | |
| SVC | 1004 | Requested version of API is deprecated. Use %1 | |
| SVC | 1005 | User does not exist: %1 | |
| SVC | 1006 | Resource %1 does not exist | **`Sim icc:<iccid> not found`** — 404 equivalent |
| SVC | 1011 | Invalid %1 length. Length should be less than %2 characters | |
| SVC | 1012 | Invalid %1 format. Allowed Charset is %2 | |
| SVC | 1013 | %1 Operation is not allowed: %2 | |
| SVC | 1020 | Needed parameter was not found. %1 | |
| SVC | 1021 | Invalid parameter value: %1. Supported values are %2 | **Invalid state transition** — `modifySubscription` returns this when target state is not reachable |
| POL | 1000 | Restricted Information: %1 | RBAC denial |
| SVR | 1000 | Generic Server Error: %1 | HTTP 500 equivalent |
| SVR | 1003 | Requested Operation is not implemented: %1 | HTTP 501 equivalent |
| SVR | 1006 | Service temporarily unavailable: system overloaded | **Treat as transient — back off** (HTTP 503 equivalent) |

Current adapter status: `app/providers/kite/client.py` parses SOAP fault detail and maps known IDs to domain errors: `SVC.1006` → 404, `SVC.1021` and validation SVC IDs → 422, `POL.1000` → 403, `SVR.1003` → unsupported operation, `SVR.1006` → retryable 503. `SOATransactionID` / `SOAConsumerTransactionID` is preserved where the domain error supports provider metadata.

---

## 2. Per-endpoint review — Tele2 (Cisco Control Center / Jasper REST API)

> **The catalog is mirrored from `tele2.jasperwireless.com/assets/documentation/lang_en/...` with English text.** Every entry below is **(D)** unless marked otherwise.

### 2.0 Foundations (D)

- **Authentication:** **HTTP Basic** (`Authorization: Basic base64(username:apiKey)`). NOT Bearer. The `username` is the Control Center username; the `apiKey` is generated per-user in the Control Center UI.
- **Base URL pattern:** `https://<YOUR-BASE-URL>/rws/api/v{apiVersion}/...` — example `https://restapi3.jasper.com/rws/api/v1/`. The Tele2 mirror likely uses a Tele2-specific host (e.g. `https://restapi.tele2.com/rws/api/v1/`). [REQUIRES INPUT: the production Tele2 host].
- **API version:** `1` for all current functions.
- **HTTPS only.** SSL must use a CA-signed certificate; Cisco recommends not pinning their cert.
- **Concurrency / rate:** "Limit the number of active calls to one at a time and avoid concurrent API processing. Do not exceed the calls per second limit." (D, but exact CPS not given). [REQUIRES INPUT: documented CPS limit per account].
- **Page size cap:** maximum **50 per page** for paginated list endpoints. (D)
- **Date format (request):** ISO 8601 e.g. `2016-04-18T17:31:34+00:00`. URL-encode the `+` as `%2B` and `:` as `%3A`.
- **Date format (response):** ISO 8601 with UTC offset, e.g. `2016-04-18 17:31:34.121+0050`; some responses also use UNIX epoch seconds.
- **Error envelope:** `{ "errorMessage": "...", "errorCode": "10000001" }`.
- **HTTP status codes:** `200` Success, `202` Accepted (asynchronous), `400` Bad request, `401` Invalid credentials, `404` Resource not found, `500` Server error.
- **Roles:** the API user must have `Access Type ∈ {API Only, Both API and UI}`. Specific functions require role gates (e.g. aggregated usage requires `AccountAdmin`).

### 2.1 `GET /rws/api/v1/devices` — Search Devices (D)

- **Capability:** `list_lines`
- **Required:** `modifiedSince` — ISO-8601 date. **Without it the API will not return all devices** — there is no "give me all devices ever" mode. The doc says: "returns a list of devices that have changed within a specific time period."
- **Optional:** `accountId, modifiedTill (≤ modifiedSince+1y), status, pageSize (≤50, default 50), pageNumber (default 1), accountCustom1..10, operatorCustom1..5, customerCustom1..5`. Wildcards `*` allowed in custom-field text filters.
- **Response:** `{ pageNumber, lastPage, totalCount, devices: [{iccid, status, ratePlan, communicationPlan, ...}] }`. Records are **sorted by modification date ascending (oldest first)**.
- **Pagination:** `pageNumber` 1-based, `pageSize` ≤ 50.
- **Incremental sync:** **`modifiedSince` IS the documented incremental-sync hook.** Persist a per-tenant high-water mark and resume from there.
- **Current adapter status:** fixed. Tele2 now uses `/rws/api/v{apiVersion}/...`, requires `username`, sends HTTP Basic `base64(username:apiKey)`, includes `modifiedSince`, and adds `modifiedTill` so each search window is at most one year.
- **Confidence:** **HIGH.**

### 2.2 `GET /rws/api/v1/devices/{iccid}` — Get Device Details (D)

- **Capability:** `get_line_detail` + plan + limits + last session info
- **Documented response fields (sample, not exhaustive):** `iccid, status, deviceID, modemID, ratePlan, communicationPlan, dateShipped, dateActivated, dateModified, accountId, accountName, customerName, ipAddress, p5gCommercialStatus, overageLimitOverride, testReadyDataLimit, testReadySmsLimit, testReadyVoiceLimit, testReadyCsdLimit, accountCustom1..10, operatorCustom1..5, customerCustom1..5, simState` (synonym of `status`).
- **Partial response:** `?fields=...` parameter selects fields to return — **but does NOT work with PUT/POST/DELETE**.
- **Confidence:** **HIGH.**

### 2.3 `GET /rws/api/v1/devices/{iccid}/usage` — Get Device Usage (D)

- **Capability:** `get_usage` (per-device)
- **Date range:** **`startDate, endDate` in `YYYYMMDD` format** (NOT ISO-8601 here). Maximum range = 30 days.
- **Metrics documented:** `data, voice, sms, vmo (voice MO), smo (SMS MO), vmt (voice MT), smt (SMS MT)`.
- **Units (D from response example):** `data → "bytes"`, `sms → "messages"`. Voice unit is implied as **seconds** by convention (Cisco Jasper across all Control Center docs uses seconds for voice usage; the snippet quoted does not literally state it). [REQUIRES INPUT: confirm `voice/vmo/vmt` unit].
- **Adapter mismatch:** adapter's `_metric_unit` returns `"minutes"` for voice (`adapter.py:191-192`). Cisco's de-facto unit is **seconds**. Fix once the unit is confirmed.
- **Confidence:** **HIGH** for shape; **MEDIUM** for voice unit.

### 2.4 `GET /rws/api/v1/dynareport/acct/usage` — Get Aggregated Usage Details (D)

- **Capability:** `get_aggregated_usage`
- **Required role:** **`AccountAdmin`**.
- **Limits:** `startDate`–`endDate` ≤ 30 days; max **15,000 rows per response**; pagination via `pageSize/pageNumber`.
- **Filters:** `groupBy ∈ [account, carrier, country, rateplan, ratingzone, ...]`, `metrics ∈ [voice, data, sms, vmo, smo, vmt, smt]`, `sortedBy`, `sortDirection`.
- **Response shape:** `{ meta: {startDate,endDate,metrics,groupBy,sortedBy,sortDirection,pagination}, data: [{groups: [{groupId,value}], metrics: [{metricType,usage,count,unit}]}] }`.
- **Adapter status:** **not implemented.**
- **Confidence:** **HIGH.**

### 2.5 `GET /rws/api/v1/devices/{iccid}/sessionDetails` — Get Session Details (D)

- **Capability:** `get_presence`
- **Response fields:** `dateSessionStarted, lastSessionEndTime, ipAddress`. (Other fields may exist; the catalog excerpts only quote these.)
- **Scope:** "current or most recent session." (D)
- **Current adapter status:** uses the canonical `/rws/api/v{api_version}/devices/{iccid}/sessionDetails` path.
- **Online rule (sound, given the fields):** `state = ONLINE` iff `ipAddress != null and lastSessionEndTime == null`; `OFFLINE` iff `lastSessionEndTime != null`; otherwise `UNKNOWN`. The adapter rule follows this.
- **Confidence:** **HIGH.**

### 2.6 `PUT /rws/api/v1/devices/{iccid}` — Edit Device Details (D, but body shape NC)

- **Capability:** `set_administrative_status` + edit `ratePlan, communicationPlan, customFields, ...`
- **HTTP verb:** PUT (also POST/DELETE for some operations — the doc's note that `?fields=` does NOT work with PUT/POST/DELETE proves these verbs are valid for edits).
- **Body fields documented in the catalog excerpts:** `status, ratePlan, communicationPlan, customFields, identifiers, effectiveDate (yyyy-MM-ddZ)`.
- **Idempotency:** the catalog excerpts do **not** mention an `Idempotency-Key` header. Cisco Control Center has no documented idempotency mechanism. The adapter sets `Idempotency-Key` defensively — harmless if Cisco ignores it.
- **Confidence:** **HIGH** for verb and URI, **MEDIUM** for body shape (full body fields not in our excerpt set).

### 2.7 SIM Status Values (D, exact)

The catalog explicitly lists 8 values: **`ACTIVATED, ACTIVATION_READY, DEACTIVATED, INVENTORY, PURGED, REPLACED, RETIRED, TEST_READY`**. **No `SUSPENDED`, no `READY`, no `ACTIVE`.**

| Native | Internal | Allows traffic? | Reactivable? | Final/destructive? | Notes |
|---|---|---|---|---|---|
| `ACTIVATED` | `active` | yes | n/a | no | |
| `ACTIVATION_READY` | `pending` (or new internal `activation_ready`) | likely no | yes | no | One-time transition gate; auto-activates on first traffic per Jasper conventions |
| `TEST_READY` | `in_test` | yes (limited) | yes | no | |
| `INVENTORY` | `inventory` | no | yes | no | |
| `PURGED` | `purged` | no | conditional — required state for "Return to Inventory" transfer | reversible only via inventory transfer | **D — error 1000512**: "SIM is not in Purged State. When you return a device to inventory, it must have the Purged SIM state." |
| `RETIRED` | `terminated` | no | NC (typically no per Jasper convention) | yes | |
| `REPLACED` | `replaced` (new internal value) | no | n/a | yes | indicates SIM swap |
| `DEACTIVATED` | `terminated` | no | yes | no | "SIM cannot be re-activated automatically — manual operation required" (Jasper convention) |

**Current adapter status:** fixed. `tele2/status_map.py` maps the eight official Cisco values and keeps `ACTIVE` / `READY` only as read-path aliases for non-standard deployments. Write-path conversion emits official Cisco values only; `SUSPENDED` is intentionally unsupported for Tele2.

### 2.8 Allowed transitions (D, partial, from error catalog)

The error catalog reveals state-machine constraints:
- `10000029 — This SIM may not be moved back to a Pre Activation status` — once activated, you can't go back to `ACTIVATION_READY`/`TEST_READY`.
- `10030000 — Cannot activate a SIM which is already activated` — idempotent re-activation is rejected (use `?fields=status` to check first).
- `1000512 — SIM is not in Purged State` (HTTP 202) — must be in `PURGED` to return to inventory.
- `1000559 — Target SIM State not allowed for Return to Inventory Transfer`.
- `1000591 — Setting P5G attributes are not allowed since P5GaaS is not enabled for the operator`.
- The **HTTP 202 codes for transfer ops indicate asynchronous processing** — "the request is scheduled for processing and the task should be complete within several seconds."

[REQUIRES INPUT: the full "SIM States" page in the catalog with the formal state-transition diagram — only the error codes were captured here].

### 2.9 Capabilities NOT in the resource catalog

- **No documented network-reset endpoint distinct from purge.** (D — by absence)
- **No documented status-history / audit-trail REST endpoint.** Audit trail is via Webhooks ("Hub URL" registration), not pull.
- **`Get Device Location`** exists separately — out of scope for line management today.
- **TMF APIs (Digital Services)** require **OAuth 2.0 with `Authorization: Bearer <ACCESS-TOKEN>`** (D) — different auth scheme from Basic. Not used today; if SIM Ordering is added, reuse a separate adapter.

### 2.10 Other catalog endpoints (for reference)

- `Devices`: `Search Devices, Get Device Details, Get Device Details by EID, Get Device Location, Get Device Usage, Get Session Details, Edit Device Details`.
- `Dynamic Reporting`: `Get Aggregated Usage Details, Get Device Subscriptions, Get Top K Usage Details, Create Device Report, Create Aggregated Usage Report, Get Report Status`.
- `SMS Messages`: `Search SMS Messages, Get SMS Details, Send an SMS`.
- `QoS on Demand`: `Get Services for Account, Activate QoS Profile, Get Job Status, Revoke QoS Profile Activation`.
- `Hub` (webhooks): `Create Hub URL, Get Registered Hub Details, Remove Hub URL`.
- `Echo`: `GET /rws/api/v1/echo/{param}` — health/debug.

---

## 3. Per-endpoint review — Moabits (Orion API 2.0)

> Source: `moabits.md` (Spanish narrative summary, sections 1–6), Orion API 2.0.0 Swagger (`https://www.api.myorion.co/api-doc`), and `app/providers/moabits/adapter.py`. Swagger confirms Bearer/JWT auth, server `https://www.api.myorion.co/`, core URIs, purge body/response, and the public write transitions (`active`, `suspend`, `purge`, `setLimits`, `update name`). Field-level payload examples are still needed for status casing and optional fields.
>
> **Strong inference:** moabits.md mirrors Cisco/Jasper terminology exactly (`ACTIVATED, DEACTIVATED, PURGED, INVENTORY, TEST_READY` — identical to Tele2 §2.7). This is consistent with Moabits being a reseller layer over a Cisco-flavored backend, but the **actual `simStatus` casing emitted by the Moabits API is unverified** — the adapter assumes lowercase, which will silently bucket valid values into `unknown` if the API uses ALL_CAPS.

### 3.1 `GET /api/sim/details/{iccidList}` (single) and `GET /api/company/simListDetail/{companyCodes}` (list) — (D)

- **Capability:** `get_line_detail` + `list_lines`
- **Doc reference:** Orion Swagger confirms both paths.
- **Output (O):** `info.simInfo[]` rows.
- **Confidence:** HIGH for path/auth; MEDIUM for full payload shape.

### 3.2 `GET /api/sim/serviceStatus/{iccidList}` and `GET /api/company/simList/{companyCodes}` (D)

- **Capability:** `get_status` (single) / list status (multi)
- **Output:** `info.iccidList[]` rows with `iccid, simStatus, dataService, smsService`.
- **simStatus values observed in adapter:** `Active, Ready, Suspended` (lowercase compare). **moabits.md SECCIÓN 1 line 7** says the values **on Moabits** are `ACTIVATED, DEACTIVATED, PURGED, INVENTORY, TEST_READY`. **One source must be wrong.** Without a real payload from production, [REQUIRES INPUT].
- **Confidence:** HIGH for path; MEDIUM/LOW for status casing until a real payload is captured.

### 3.3 `GET /api/usage/simUsage` (D)

- **Capability:** `get_usage`
- **Doc reference:** Orion Swagger confirms the URI. The adapter sends `iccidList`, `initialDate`, and `finalDate`; keep these under payload validation until a Swagger/request sample is captured.
- **Output (O):** `info.simsUsage[]` with `iccid, activeSim, smsMO, smsMT, data` (in MB per adapter comment).
- **Voice usage:** **not exposed.**
- **Aggregated usage:** Orion Swagger confirms `GET /api/usage/companyUsage`; **not exposed by backend v1**.
- **Confidence:** MEDIUM.

### 3.4 `GET /api/sim/connectivityStatus/{iccidList}` (D)

- **Capability:** `get_presence`
- **Doc reference:** Orion Swagger confirms the URI. The current adapter passes one ICCID in the `{iccidList}` path slot.
- **Output:** `info.connectivityStatus[0]` with `iccid, status (online|offline), country, rat, network`. **No IP address in the response.**
- **Real-time vs cached:** [REQUIRES INPUT].
- **Confidence:** HIGH for path; MEDIUM for response freshness/shape.

### 3.5 `PUT /api/sim/{active|suspend|purge}/` (D)

- **Capability:** `set_administrative_status` (limited) + administrative purge
- **Body:** `{iccidList: [iccid], dataService?: bool, smsService?: bool}`.
- **Doc reference:** Orion Swagger confirms `PUT /api/sim/purge/` with body `{ "iccidList": ["..."] }`; the generic `{status: PURGED}` body does not exist in the public spec.
- **Purge response:** `200 {"status":"Ok","info":{"purged":true}}`; false means the provider did not confirm purge.
- **Documented errors:** 400 (no iccid list), 401 (absent authorization), 403 (access denied), 500 (client not found).
- **Writable status transitions:** only active, suspend, and purge. No public endpoint for TEST_READY, DEACTIVATED, or INVENTORY.
- **Confidence:** HIGH for route/body/response.

### 3.6 What moabits.md names but the adapter does NOT implement

| moabits.md operation | Adapter |
|---|---|
| `Get Aggregated Usage Details` | URI confirmed as `GET /api/usage/companyUsage`; not exposed by backend v1 |
| Set limits | URI confirmed as `PUT /api/sim/setLimits/`; not exposed by backend v1 |
| Update SIM name | URI confirmed as `PUT /api/sim/details/{iccid}/name/`; not exposed by backend v1 |
| `Get Service Specification` | not confirmed in supplied Swagger extract |
| Status history per SIM | not in moabits.md → not implemented |

---

## 4. Global Output A — Provider capability matrix (REVISED)

| Capability | Kite | Tele2 | Moabits |
|---|---|---|---|
| `list_lines` | direct (`getSubscriptions`, `searchParameters` rich) | direct (`GET /rws/api/v1/devices`) — **`modifiedSince` required** | composed (per-companyCode fan-out, no native pagination) |
| `get_line_detail` | composed (`getSubscriptionDetail` + `getSubscriptions[icc]` for consumption + `getStatusDetail` + `getPresenceDetail`) | direct (`GET /rws/api/v1/devices/{iccid}`) | composed (`details` + `serviceStatus`) |
| `get_status` | direct (`getStatusDetail`) | embedded in detail (`status` field) | embedded (`serviceStatus.simStatus`) |
| `get_status_history` | direct (`getStatusHistory` with date range) | **not_supported** (no audit-trail REST endpoint; available via Hub webhooks) | not_supported |
| `get_presence` | direct (`getPresenceDetail`) | direct (`GET /rws/api/v1/devices/{iccid}/sessionDetails`) | direct (`/api/sim/connectivityStatus/{iccidList}`) |
| `get_usage` (single) | embedded in `getSubscriptions` (NOT in `getSubscriptionDetail`) | direct (`GET /rws/api/v1/devices/{iccid}/usage`, ≤30 days) | direct (`GET /api/usage/simUsage`, with date range) |
| `get_aggregated_usage` (account) | composed (`Reports` async) | direct (`GET /rws/api/v1/dynareport/acct/usage`, `AccountAdmin` role) | documented (`GET /api/usage/companyUsage`) but not exposed by backend v1 |
| `get_plan_details` | embedded (`servicePack`, `commercialGroup`, `basicServices`, `supplServices`) | embedded (`ratePlan`, `communicationPlan`) | embedded (`product_*`, `services`) |
| `get_limits` | embedded + edit via `modifySubscription{*ConsumptionThreshold, monthlyExpenseLimit}` (units: bytes/seconds/SMS-amount) | embedded on detail (`overageLimitOverride`, `testReady*Limit`) | embedded (`dataLimit`, `smsLimit`) |
| `network_reset` | direct (`networkReset`, per-radio flags `network2g3g`/`network4g`) | **not_supported** (no documented endpoint distinct from purge) | not_supported as a distinct operation |
| `administrative_retire / purge` | constrained — `modifySubscription{lifeCycleStatus}` accepts only `{INACTIVE_NEW, TEST, ACTIVATION_READY, ACTIVATION_PENDANT, ACTIVE}`; **`SUSPENDED, DEACTIVATED, RETIRED, RESTORE` are NOT settable via this op** (D-doc); RETIRED requires the SIM to already be SUSPENDED (operator action) | direct (`PUT /devices/{iccid} {status: PURGED}`); state-machine enforced; "Return to Inventory" requires PURGED state | direct (`PUT /api/sim/purge/` with `iccidList`) |
| `user_management` | direct (User v3 services) — out of scope | not_confirmed (admin UI only?) | not_confirmed |
| `incremental_sync` | composed via `searchParameters[startLastStateChangeDate, endLastStateChangeDate, ...]` and async `Reports` | **direct via `modifiedSince`** — required parameter on Search Devices | not_supported |
| `authentication` | **mTLS/client certificate** (D-doc evidence); optional WSSE only if deployment configures both username/password | **HTTP Basic with `username:apiKey`** (D) | Bearer/JWT per Orion Swagger; `GET /integrity/authorization-token` returns a JWT |
| `pagination` | direct (`startIndex`+`maxBatchSize`, max 1000) | direct (`pageNumber`+`pageSize`, max 50) | not_supported by provider — **client-side over full pulls** |
| `error_handling` | rich (D table of `SVC/POL/SVR` `exceptionId`s + `SOATransactionID`) | standard HTTP + `{errorCode, errorMessage}` body | basic HTTP |
| `rate_limit_signaling` | `SVR 1006` (overload), no headers documented | "do not exceed CPS" (no exact CPS, no headers) | NC |
| `webhook / push` | not_confirmed | direct (`Create Hub URL`) — out of scope today | not_confirmed |

---

## 5. Global Output B — Internal backend API recommendation (REVISED)

### `GET /v1/sims`

- **Kite:** `getSubscriptions(startIndex=cursor, maxBatchSize=min(limit,1000))`. Pass through any `searchParameters` from the caller (whitelisted set).
- **Tele2:** `GET /rws/api/v1/devices?modifiedSince=<persisted-watermark>&modifiedTill=<window-end>&pageNumber=cursor&pageSize=min(limit,50)`. **`modifiedSince` is required**, and `modifiedTill` must keep each query window at one year or less. For "first sync" use a low default (e.g. 2010-01-01) and advance the cursor through one-year windows.
- **Moabits:** fan-out over `company_codes` and slice locally, capped at `limit ≤ 500`.
- **Required normalization:** ICCID (canonical), IMSI/MSISDN where available, `status` (normalized), `native_status`, `provider`, plan summary, `updated_at`.

### `GET /v1/sims/{iccid}`

- **Kite:** compose (`getSubscriptionDetail` + `getSubscriptions[icc]` + `getStatusDetail` + `getPresenceDetail`) — explicit cost = up to 4 SOAP calls.
- **Tele2:** single `GET /rws/api/v1/devices/{iccid}` (1 call) + optional `GET .../sessionDetails` (2nd call only if presence requested).
- **Moabits:** parallel `details + serviceStatus` (already implemented).

### `GET /v1/sims/{iccid}/usage`

- **Kite:** `getSubscriptions(searchParameters[icc=<id>], maxBatchSize=1)` and read `consumptionMonthly`. **NOT `getSubscriptionDetail`** — that response type does not include consumption.
- **Tele2:** `GET /rws/api/v1/devices/{iccid}/usage?startDate=YYYYMMDD&endDate=YYYYMMDD` (≤30 days; default to current cycle).
- **Moabits:** `GET /api/usage/simUsage?iccidList=<id>&initialDate=...&finalDate=...`.
- **Unit normalization at the adapter boundary:**
  - `data → bytes` everywhere.
  - `voice → seconds` everywhere (Kite: native `seconds`; Tele2: native unit ambiguous → confirm; Moabits: voice not exposed).
  - `sms → count` everywhere.
- **Internal field rename:** rename `Subscription.data_used_mb → data_used_bytes` to avoid the bug-prone unit ambiguity (Tele2 returns bytes, Moabits MB; current single field name is misleading).

### `GET /v1/sims/{iccid}/presence`

- **Kite:** `getPresenceDetail(icc)`. **Map `level=GSM` to `unknown`/`voice_only`, NOT `online`** — this is the current adapter bug (`mappers.py:367-370`).
- **Tele2:** `GET /rws/api/v1/devices/{iccid}/sessionDetails`. Single canonical path; the adapter now uses this directly.
- **Moabits:** `GET /api/sim/connectivityStatus/{iccidList}`; backend passes one ICCID in that path slot.

### Capability `status_history` (not a public v1 route today)

- **Kite:** `getStatusHistory(icc, startDate?, endDate?)`. Default to last 90 days.
- **Tele2:** **not_supported** — no audit-trail endpoint in the resource catalog. Hub URL webhooks can be subscribed for live changes (separate feature).
- **Moabits:** **not_supported.**
- Current backend contract: report this through `GET /v1/providers/{provider}/capabilities`; do not expose `GET /v1/sims/{iccid}/status-history` until a `HistoryProvider` protocol and router are implemented.

### Native `networkReset` (implemented only behind canonical purge)

- **Kite:** `networkReset(icc, network2g3g=true, network4g=true)`. Body: `{ target_radios?: ["2g3g","4g"] }`.
- **Tele2:** **not_supported as a separate native operation** — no documented endpoint apart from lifecycle edit.
- **Moabits:** no separate network reset; canonical purge maps to confirmed `PUT /api/sim/purge/`.
- Current backend contract: keep `POST /v1/sims/{iccid}/purge` as the only public purge/network reset control operation; no `:network-reset` custom verb.

### Lifecycle change (implemented as `PUT /v1/sims/{iccid}/status`)

- **Body:** `{ target: "active" | "in_test" | "activation_ready" | "activation_pendant" | "inactive_new" | "purged" | "retired" | "deactivated" | "inventory" | "replaced", reason: string, idempotency_key: string }`.
- **Kite:** `modifySubscription(icc, lifeCycleStatus=<UPPER>)` — **only `INACTIVE_NEW, TEST, ACTIVATION_READY, ACTIVATION_PENDANT, ACTIVE` are settable**. Reject `purged|retired|deactivated|suspended` with 422 + `provider_capability=not_supported_by_provider_for_this_target`.
- **Tele2:** `PUT /rws/api/v1/devices/{iccid}` with `{status: <NATIVE>, effectiveDate?: yyyy-MM-ddZ}`. Allowed natives: `ACTIVATED, ACTIVATION_READY, DEACTIVATED, INVENTORY, PURGED, RETIRED, TEST_READY`. **Do NOT send `SUSPENDED` — the state does not exist in Cisco.**
- **Moabits:** `PUT /api/sim/active|suspend|purge/` for `active|suspended|purged`. Orion Swagger confirms no public endpoint for `TEST_READY`, `DEACTIVATED`, or `INVENTORY` writes; `in_test` remains not_supported.
- Current backend contract: expose normal status writes through `PUT /v1/sims/{iccid}/status` and destructive purge through `POST /v1/sims/{iccid}/purge`; no `:lifecycle-change` custom verb.

### Account usage / aggregated usage (not a public v1 route today)

- **Kite:** enqueue `Reports.createDownloadReport`; expose async job.
- **Tele2:** `GET /rws/api/v1/dynareport/acct/usage` (requires `AccountAdmin` role). **Not implemented today.**
- **Moabits:** `GET /api/usage/companyUsage` exists in Orion Swagger. **Not exposed by backend v1.**
- Current backend contract: report as `aggregated_usage` capability; keep per-SIM usage at `GET /v1/sims/{iccid}/usage`.

---

## 6. Global Output C — Normalized data model (REVISED)

Same as the prior draft. Two concrete renames are now strongly recommended given documented units:

1. `Subscription.data_used_mb` → **`Subscription.data_used_bytes`** (Tele2 returns bytes; Moabits MB; Kite bytes — single canonical unit at the boundary).
2. `Subscription.voice_minutes` → **`Subscription.voice_seconds`** (Kite glossary documents seconds; Tele2 unit unconfirmed; Moabits doesn't expose voice).

Add a normalized status enum value: **`activation_ready`** and **`activation_pendant`** (Kite-specific but useful) and **`replaced`** (Tele2-specific). The internal `LineStatus.allows_traffic` field can then be derived deterministically from the normalized state without provider branching.

---

## 7. Global Output D — Status normalization table (REVISED)

| Provider | Native | Meaning (D) | Internal | Allows traffic? | Reactivable? | Final/destructive? |
|---|---|---|---|---|---|---|
| **Kite** | `INACTIVE_NEW` | initial state, no traffic, immutable until commercial group set | `inactive_new` | no | yes | no |
| Kite | `ACTIVATION_PENDANT` | requires manual operation to transition to active | `activation_pendant` | no | yes | no |
| Kite | `ACTIVATION_READY` | auto-activates on first traffic session | `activation_ready` | yes (briefly, transitions to `ACTIVE` on traffic) | yes | no |
| Kite | `TEST` | free traffic up to limits | `in_test` | yes (limited) | yes | no |
| Kite | `ACTIVE` | normal operation | `active` | yes | n/a | no |
| Kite | `SUSPENDED` | no traffic; can only return to previous state; **not settable via API** | `suspended` | no | only to previous state | no |
| Kite | `DEACTIVATED` | terminated; can be re-activated manually | `terminated` | no | yes (manual) | no |
| Kite | `RETIRED` | removed from M2M Platform; ICC/IMSI/MSISDN reusable; **only settable from SUSPENDED** | `retired` | no | no | yes |
| Kite | `RESTORE` | restoration in progress | `restore` | no | n/a | no |
| **Tele2** | `ACTIVATED` | active | `active` | yes | n/a | no |
| Tele2 | `ACTIVATION_READY` | one-time gate; auto-activates on first traffic | `activation_ready` | yes (limited) | "no Pre-Activation rollback" once activated | no |
| Tele2 | `TEST_READY` | trial | `in_test` | yes (limited) | yes | no |
| Tele2 | `INVENTORY` | provisioned but not in service | `inventory` | no | yes | no |
| Tele2 | `PURGED` | purged; required state for "Return to Inventory" transfer | `purged` | no | only via inventory transfer | conditional |
| Tele2 | `RETIRED` | retired | `terminated` | no | NC | yes |
| Tele2 | `REPLACED` | replaced (SIM swap) | `replaced` | no | n/a | yes |
| Tele2 | `DEACTIVATED` | deactivated | `terminated` (or `deactivated` if separate semantics needed) | no | NC | NC |
| **Moabits** | `Active`/`ACTIVATED` | active | `active` | yes | n/a | no |
| Moabits | `Ready`/`TEST_READY` | trial/provisioned | `in_test` | yes (limited) | yes | no |
| Moabits | `Suspended` | admin suspended | `suspended` | no | yes | no |
| Moabits | `Purged`/`PURGED` | required to return to inventory (D-MD §6) | `purged` | no | yes (to inventory) | conditional |
| Moabits | `Inventory`/`INVENTORY` | (D-MD §1 line 7) | `inventory` | no | yes | no |
| Moabits | `Deactivated`/`DEACTIVATED` | (D-MD §1 line 7) | `terminated` | no | NC | NC |

**Action:** rewrite all three `status_map.py` files to handle the full enum; default `unknown` only for truly unknown values. **Remove the bogus `READY` and `SUSPENDED` entries from the Tele2 map.**

---

## 8. Global Output E — Usage normalization (REVISED)

| Question | Kite | Tele2 | Moabits |
|---|---|---|---|
| Endpoint embedded vs separate | embedded in `getSubscriptions` | separate | separate |
| Cycle scope | "current daily" + "current monthly" + "current expenseMonthly" (provider-defined) | provider-defined cycle, ≤30 days range query | open date range, no cap stated |
| Historical | NO (current daily + monthly only); for historical use `Reports` | yes via `startDate/endDate` (≤30 days) | yes |
| Aggregated | `Reports.createDownloadReport` (async) | `dynareport/acct/usage` (requires `AccountAdmin`) | `GET /api/usage/companyUsage` (not exposed by backend v1) |
| Metrics | `voice, sms, data` | `data, voice, sms, vmo, smo, vmt, smt` | `data, smsMO, smsMT` (no voice) |
| Units | **D**: data=bytes, voice=seconds, sms=SMS amount/count | **D**: data=bytes, sms=messages, **voice=NC (likely seconds)** | **O**: data=MB, sms=count |
| Direction available? | NO (only voice/sms aggregate) | yes (`*mo`/`*mt`) | partial (`smsMO`/`smsMT`) |
| Date range | NO (current period only) | yes (`startDate`, `endDate`, ≤30 days) | yes (`initialDate`, `finalDate`) |
| Limits returned with usage? | yes (`limit`, `enabled`, `trafficCut`) | NO (limits on detail) | NO (limits on detail) |
| Traffic cut returned? | yes (`trafficCut: bool`) | NO | NO |

---

## 9. Global Output F — Presence / session normalization (REVISED)

| Question | Kite | Tele2 | Moabits |
|---|---|---|---|
| Endpoint | `getPresenceDetail` | `GET /rws/api/v1/devices/{iccid}/sessionDetails` | `GET /api/sim/connectivityStatus/{iccidList}` |
| Online indicator | `level ∈ {GPRS, "IP reachability"}` | `ipAddress != null AND lastSessionEndTime == null` | `status == "online"` |
| IP returned | yes (`ip`) | yes (`ipAddress`) | NO |
| APN returned | yes (`apn`) | NC | NO |
| Session start | NO (only presence `timeStamp`) | yes (`dateSessionStarted`) | NO |
| Last session end | NO | yes (`lastSessionEndTime`) | NO |
| Cause/reason | yes (`cause`, only example documented: `UNKNOWN_SUBSCRIBER`) | NC | NO |
| Real-time vs cached | last-known with `timeStamp`; `gprsStatus.status` is documented as "Real-time status" | "current or most recent session" | NC |
| RAT | yes (`ratType` int, with documented value table) | NC | yes (`rat`) |
| Network/country | NO | NO | yes (`network`, `country`) |

**Online rule (FINAL):**
- **Kite:** ONLINE iff `level == "GPRS"` or `level == "IP reachability"`. **`GSM` → NOT online** — the SIM is camped on 2G voice without a data session.
- **Tele2:** ONLINE iff `ipAddress != null AND lastSessionEndTime == null`.
- **Moabits:** ONLINE iff `status == "online"` (case-insensitive).

---

## 10. Global Output G — Network reset vs administrative purge (REVISED)

| Aspect | Kite | Tele2 | Moabits |
|---|---|---|---|
| Has technical network reset? | **YES** — `networkReset` (per-radio `network2g3g`, `network4g`); does NOT change `lifeCycleStatus` | **NO** — no documented endpoint distinct from `Edit Device Details`; **(D — by absence in resource catalog)** | **NO** distinct endpoint in Orion Swagger |
| Has administrative retire / purge? | **constrained** — `modifySubscription{lifeCycleStatus}` accepts only `{INACTIVE_NEW, TEST, ACTIVATION_READY, ACTIVATION_PENDANT, ACTIVE}`; you **cannot** API-set SUSPENDED, DEACTIVATED, or RETIRED in Kite | **YES** — `PUT /devices/{iccid} {status: PURGED}`; state-machine enforced; "Return to Inventory" requires `PURGED` first | **YES** — `PUT /api/sim/purge/` with `{iccidList:[...]}` and `info.purged=true` |
| Reversibility | Network reset: yes. Retire (when reachable via ops workflow): no. | Network reset: N/A. Purge: only via inventory transfer; not free-form reactivation. | Network reset: N/A. No public reactivation from PURGED shown in Swagger. |
| Conflation risk in adapter | Accepted product tradeoff — `KiteAdapter.purge` calls documented `networkReset` and now documents that it does not change `lifeCycleStatus`. | low — single endpoint per concept | low — dedicated purge route confirmed |

**Current internal API decision:**
- The backend exposes one canonical `POST /v1/sims/{iccid}/purge` operation.
- Provider adapters map that operation to their closest documented control primitive: Kite `networkReset`, Tele2 `Edit Device Details {status: PURGED}`, and Moabits `PUT /api/sim/purge/`.
- The operation remains gated by `LIFECYCLE_WRITES_ENABLED` and requires `Idempotency-Key` at the API boundary.

---

## 11. Global Output H — Backend validation checklist (REVISED)

| Item | Kite | Tele2 | Moabits |
|---|---|---|---|
| Calls a documented endpoint | ✅ all calls map to documented operations | ✅ `/rws/api/v1/...` paths and canonical `sessionDetails` URL | ✅ core read/write paths confirmed in Orion Swagger |
| Avoids unsupported endpoints | ✅ Kite writes only documented `modifySubscription` target subset | ✅ | ✅ |
| Preserves native status | ✅ | ✅ | ✅ |
| Calculates normalized status separately | ✅ | ✅ official Cisco enum covered; `ACTIVE`/`READY` retained as read aliases only | ⚠️ casing unverified |
| Handles optional fields safely | ✅ | ✅ | ✅ |
| Validates destructive ops | ⚠️ no UI-level confirmation enforced | ⚠️ same | ⚠️ same |
| Distinguishes technical reset from admin retire | ⚠️ intentionally unified as canonical `purge`; Kite docstring calls out `networkReset` semantics | ✅ | ✅ |
| Pagination correct | ✅ `1..1000` matches doc | ✅ `pageSize<=50`, `pageNumber`, `modifiedSince`, one-year `modifiedTill` window | ⚠️ in-memory pagination — memory risk for large accounts |
| Avoids N+1 in list | ✅ | ⚠️ — see above | ⚠️ |
| No presence/usage call per row | ✅ | ✅ | ✅ |
| Auth scheme correct | ✅ mTLS PFX support; WS-Security UsernameToken optional when configured | ✅ HTTP Basic `username:apiKey` | ✅ Bearer/JWT per Orion Swagger |
| Voice unit | ⚠️ raw integer in `provider_fields`; not normalized (doc: seconds) | ⚠️ adapter says `minutes`; doc is most likely seconds (Cisco convention) | N/A (voice not exposed) |
| Data unit | ✅ canonical `data_used_bytes`; raw provider data preserved in `provider_metrics` | ✅ canonical `data_used_bytes` | ✅ native MB converted to canonical bytes; `data_mb` preserved in `provider_metrics` |
| Logs provider request IDs | ⚠️ Kite fault transaction IDs surfaced in domain errors; happy-path audit still pending | ⚠️ Tele2 `errorCode`/`errorMessage` surfaced on errors; request-id audit pending | ❌ |
| Handles provider-specific errors | ✅ known SOAP fault IDs mapped | ✅ HTTP + `{errorCode,errorMessage}` mapped | ⚠️ |
| `modifiedSince` for incremental sync | ❌ not used | ✅ used with `modifiedTill` one-year windows | N/A (provider has no incremental) |

---

## 12. Global Output I — Provider-specific open questions (REVISED — much shorter now)

### Kite — remaining items

- Authentication policy confirmation in writing from Telefónica for this tenant (binding evidence is mTLS/client certificate; WSSE is now optional compatibility only).
- `forceRetired` flag semantics (in WSDL XSD, not in spec excerpts).
- Whether `RETIRED` and `DEACTIVATED` are reachable via API at all, or only via portal workflow.
- `cause` enumeration (only `UNKNOWN_SUBSCRIBER` is given as example).
- `getStatusHistory` server-side cap on number of records.
- Concrete CPS / rate-limit value (only "do not exceed CPS limit" stated).
- Whether `searchParameters` accepts multiple `(name,value)` pairs as AND filters (the doc shows a sequence).

### Tele2 — remaining items

- Production base host for the Tele2 mirror (`https://restapi.tele2.com/rws/api/v1/`?).
- Voice usage unit (assumed seconds per Cisco convention; literal text not in our excerpt).
- The full **SIM States** transition diagram (only specific error codes were captured).
- Documented CPS limit value.
- Whether `Idempotency-Key` is honored or silently ignored.

### Moabits — remaining items

- Real `serviceStatus` payload to settle simStatus casing.
- Full request/response shape for `GET /api/usage/companyUsage`.
- Full request/response shape for `PUT /api/sim/setLimits/` and `PUT /api/sim/details/{iccid}/name/` before exposing quota/name writes.
- Whether `connectivityStatus` is real-time or cached.
- Sample payload to verify all field names used by the adapter (`product_*`, `services` slash-separated, `dataLimit/smsLimit`).

---

## 13. Global Output J — Final recommendation (REVISED)

### Safe to keep / implement now (HIGH confidence)

- **Kite:** `getSubscriptions, getSubscriptionDetail, getPresenceDetail, getStatusDetail, getStatusHistory, networkReset` — all documented, all matching local WSDL.
- **Kite:** **implement `set_administrative_status`** for the 5 documented target states only (`INACTIVE_NEW, TEST, ACTIVATION_READY, ACTIVATION_PENDANT, ACTIVE`); reject the rest at the adapter boundary with a clear error.
- **Tele2:** `GET /rws/api/v1/devices` (with required `modifiedSince` and one-year `modifiedTill` window), `GET /rws/api/v1/devices/{iccid}`, `GET /rws/api/v1/devices/{iccid}/usage`, `GET /rws/api/v1/devices/{iccid}/sessionDetails`.
- **Moabits:** read paths (`details, serviceStatus, simUsage, connectivityStatus`) and purge route/body are confirmed by Orion Swagger. Status casing and optional field shapes still need a real payload.

### Already remediated in code and covered by tests

1. ✅ **Tele2 auth scheme** — `Authorization: Basic base64(username:apiKey)`; `username` is part of `Tele2Credentials`.
2. ✅ **Tele2 base path** — all adapter calls go through `/rws/api/v{api_version}/...`.
3. ✅ **Tele2 status map** — official Cisco enum covered; `SUSPENDED` unsupported on write; `ACTIVE`/`READY` kept only as read aliases.
4. ✅ **Tele2 list call** — sends `modifiedSince` and `modifiedTill`; first-sync default is `2010-01-01T00:00:00+00:00`; cursor chunks by one-year windows.
5. ✅ **Tele2 usage URL** — uses documented `/devices/{iccid}/usage` behind the shared `/rws/api/v{api_version}` prefix.
6. ✅ **Tele2 session URL** — uses canonical `/devices/{iccid}/sessionDetails`.
7. ✅ **Kite usage path** — reads consumption from `getSubscriptions(searchParameters={"icc": iccid}, maxBatchSize=1)`.
8. ✅ **Kite presence rule** — `GPRS` and `IP reachability` are online; `GSM` is not online.
9. ✅ **Kite `purge` documentation** — product decision is to keep canonical `purge()` and document that Kite maps it to `networkReset`, not an administrative lifecycle purge.
10. ✅ **Kite fault parsing** — known SOAP fault IDs map to typed domain errors and preserve provider transaction IDs where available.
11. ✅ **Kite cert-only auth support** — PFX/mTLS credentials can be used without `username/password`; WSSE is emitted only when both are configured.
12. ✅ **Kite getSubscriptions WSDL order** — request body now follows `maxBatchSize`, `startIndex`, `searchParameters`.

### Behind feature flags (not on by default)

- **Kite lifecycle change via `modifySubscription`** — flagged per tenant; even with the documented 5 target states, transition rules vary per `Basic Services Commercial Plan` and the API enforces them via `SVC.1021`.
- **Tele2 lifecycle change** — flagged until the full state-transition diagram is captured.
- **Moabits aggregated usage / limits / name update** — Swagger confirms URIs, but backend v1 does not expose these optional capabilities.

### Should return `not_supported` until confirmed

- Tele2 `status-history` (no audit-trail endpoint in the catalog).
- Tele2 `network-reset` (no documented endpoint).
- Moabits `network-reset`.
- Moabits aggregated usage in backend v1, despite confirmed provider URI.
- Moabits `status-history`.

### Production gating before any state change traffic

| Item | Provider | Action |
|---|---|---|
| Real payload sample for `serviceStatus` | Moabits | Settle status casing |
| Real payload sample for `simUsage` / `companyUsage` | Moabits | Confirm units/date params |
| Production host | Tele2 | `restapi.tele2.com/rws/api/v1`? — confirm |
| `forceRetired` semantics | Kite | Don't expose |
| Tele2 idempotency | Tele2 | Don't rely on it; persist a backend ledger of state changes |
| CPS limits | All three | Configure circuit-breaker thresholds per provider |

---

## 14. Remediation checklist status

These were the mechanical fixes justified by the documented evidence above. They are now implemented unless explicitly marked pending.

### Tele2

**File: `app/providers/tele2/adapter.py`**

- ✅ Basic auth with required `username:api_key`.
- ✅ `/rws/api/v{api_version}/...` path helper.
- ✅ Single canonical `/devices/{iccid}/sessionDetails` path.
- ✅ `modifiedSince` plus one-year `modifiedTill` windows in `list_subscriptions`.
- ⚠️ Voice unit still needs vendor confirmation before a canonical rename from minutes to seconds.

**File: `app/providers/tele2/status_map.py`**

- ✅ `_TO_CANONICAL` and `_TO_NATIVE` follow the official Cisco enum. `SUSPENDED` is unsupported on write.

**File: `app/providers/tele2/dto.py`**

- ✅ `username` field documented.

### Kite

**File: `app/providers/kite/adapter.py`**

- ✅ `get_usage` reads `consumptionMonthly` from `getSubscriptions(searchParameters={"icc": iccid}, maxBatchSize=1)`.
- ✅ `set_administrative_status` calls `modifySubscription` and sends provider field `lifeCycleStatus`.
- ✅ Allowed write targets are the documented subset: `ACTIVE`, `TEST`, `ACTIVATION_PENDANT`, `ACTIVATION_READY`, `INACTIVE_NEW`.
- ✅ `purge()` is kept as the canonical backend operation by product decision, and the Kite adapter documents that it maps to `networkReset`.
- ✅ Kite PFX/mTLS credential support added through encrypted `credentials_enc` JSON.

**File: `app/providers/kite/mappers.py`**

- ✅ `parse_presence_fields` implements the online rule: ONLINE iff `level in {"GPRS", "IP", "IP reachability"}`. `GSM` → OFFLINE. `unknown` → UNKNOWN.

**File: `app/providers/kite/client.py`**

- ✅ Fault parsing extracts known Kite fault data and maps documented errors to typed domain errors.
- ✅ PFX/mTLS is loaded from encrypted credential JSON; `UsernameToken` is optional and only included when both `username` and `password` exist.
- ✅ `getSubscriptions` emits XML in WSDL order: `maxBatchSize`, `startIndex`, `searchParameters`.

### Moabits

**File: `app/providers/moabits/status_map.py`**

- Add SCREAMING_SNAKE entries (`ACTIVATED, TEST_READY, SUSPENDED, PURGED, INVENTORY, DEACTIVATED`) alongside the current CamelCase ones, and lowercase-compare in `map_status`. This is a low-risk additive change that protects against either casing convention. Real verification still pending a sample payload.

---

## 15. Summary of confidence levels (REVISED)

| Provider | Documented today | Code matches doc | Production-ready (post-fix) |
|---|---|---|---|
| Kite | HIGH (WSDL + binding spec) | mostly aligned after remediation; remaining open items are vendor policy/operations questions | ready for sandbox validation behind feature flag |
| Tele2 | HIGH (resource catalog mirrored from `tele2.jasperwireless.com`) | aligned for catalog REST paths/auth/status/listing after remediation | ready for sandbox validation behind feature flag |
| Moabits | MEDIUM-HIGH (Orion Swagger confirms auth, server, core paths, purge body/response; payload samples still incomplete) | aligned for confirmed paths; status casing and optional field shapes need real payloads | ready for controlled smoke validation behind feature flag, not broad production writes |
