# Kite WSDL Capability Catalog

**Status:** 2026-05-07  
**Purpose:** Local reference for Kite UNICA SOAP WSDL/XSD capabilities, so the team does not need to re-query NotebookLM or re-scan every XML file when deciding what backend endpoints or provider capabilities to implement.

This document is intentionally product-oriented. It treats the Kite WSDL package as a native capability inventory, then maps each family to the backend's provider-neutral model. The public API should remain canonical (`/v1/sims/**`, `/v1/providers/{provider}/capabilities`, future capability routes) instead of exposing Kite-specific routes to the frontend.

## Source Files

The Kite WSDL package lives under:

`app/providers/kite/wsdl/`

Main service files:

| Family | Service WSDL | Type XSD | Role |
|---|---|---|---|
| Echo | `UNICA_API_SOAP_Globalm2m_echo_services_v1_0.wsdl` | `UNICA_API_SOAP_Globalm2m_echo_types_v1_0.xsd` | SOAP connectivity/healthcheck. |
| Inventory | `UNICA_API_SOAP_Globalm2m_inventory_services_v12_0.wsdl` | `UNICA_API_SOAP_Globalm2m_inventory_types_v12_0.xsd` | SIM/subscription operations. This is the main family for SIM pages. |
| EndCustomer | `UNICA_API_SOAP_Globalm2m_endcustomer_services_v2_0.wsdl` | `UNICA_API_SOAP_Globalm2m_endcustomer_types_v2_0.xsd` | Kite customer/account management. |
| User | `UNICA_API_SOAP_Globalm2m_user_services_v3_0.wsdl` | `UNICA_API_SOAP_Globalm2m_user_types_v3_0.xsd` | Remote Kite user administration. |
| Reports | `UNICA_API_SOAP_Globalm2m_reports_services_v1_16.wsdl` | `UNICA_API_SOAP_Globalm2m_reports_types_v1_16.xsd` | Async report creation/list/download. |
| Common | `UNICA_API_SOAP_common_*`, `UNICA_API_SOAP_Globalm2m_types_*` | Shared XSDs | Shared data types, addresses, contacts, identifiers, and SOAP faults. |

Version notes are in `app/providers/kite/wsdl/readme.txt`. The current package includes Inventory v12.0, EndCustomer v2.0, User v3.0, Reports v1.16, and Echo v1.0.

## Implementation Rule

Use Kite WSDL operations as adapter internals or as evidence for optional capabilities. Do not create public endpoints like `/v1/kite/...` unless there is a deliberate provider-admin feature that cannot be modeled canonically.

Recommended flow:

1. Identify the native Kite operation.
2. Check whether Tele2 and Moabits expose an equivalent.
3. If equivalent enough, add a canonical capability and adapter protocol.
4. If Kite-only, expose it as an optional capability with `not_supported` for other providers.
5. Keep native fields under `provider_fields` when they are useful but not canonical.

## Inventory Family

Inventory is the relevant WSDL family for the SIM detail page and most SIM actions.

| Kite operation | Kind | Purpose | Current backend status | Normalization guidance |
|---|---|---|---|---|
| `getSubscriptions` | Read | Search/list subscriptions with `maxBatchSize`, `startIndex`, and `searchParameters`. | Implemented by `KiteAdapter.list_subscriptions`. | Canonical `GET /v1/sims?provider=kite`. Equivalent to Tele2 Search Devices and Moabits simList/enrichment. |
| `getSubscriptionDetail` | Read | Get detailed subscription data by `icc`, `imsi`, `msisdn`, or `subscriptionId`. | Implemented by `KiteAdapter.get_subscription`. | Canonical `GET /v1/sims/{iccid}`. |
| `getPresenceDetail` | Read | Get network presence details such as level, timestamp, cause, IP/APN when present. | Implemented by `KiteAdapter.get_presence`. | Canonical `GET /v1/sims/{iccid}/presence`. Equivalent to Tele2 `sessionDetails` and Moabits `connectivityStatus`. |
| `getLocationDetail` | Read | Get manual/automatic location details and coordinates. | Not implemented. | Good candidate for `GET /v1/sims/{iccid}/location`. Tele2 has `Get Device Location`; Moabits support is unconfirmed. |
| `getStatusDetail` | Read | Get current status state, automatic flag, reason, and current status date. | Implemented in adapter, not exposed publicly. | Could enrich `GET /v1/sims/{iccid}` or support a future `GET /status-detail`; avoid if status data is already enough. |
| `getStatusHistory` | Read | Get status transitions with state, automatic flag, time, reason, and user. | Implemented in adapter, no public route. | Best next optional capability: `GET /v1/sims/{iccid}/status-history`. Tele2/Moabits should return `not_supported`. |
| `modifySubscription` | Write | Modify lifecycle state, alias/custom fields, commercial/supervision groups, LTE/QCI/VoLTE, APNs/static IPs, thresholds, service flags, and feature flags. | Partially implemented only for lifecycle subset. | Split into smaller capabilities. Do not expose the full native surface as one generic patch. |
| `getTimeAndConsumption` | Read | Get time/consumption voucher information for a subscription. | Not implemented. | Kite-specific until product confirms equivalent semantics. Keep out of core SIM usage. |
| `modifyTimeAndConsumption` | Write | Modify time/consumption voucher information. | Not implemented. | Kite-specific. Requires product decision and strong validation. |
| `sendSMS` | Write | Send SMS to one or more subscriptions; can request delivery report. | Not implemented. | Possible `sms_send` capability. Tele2 has SMS APIs/add-on; Moabits support unconfirmed. |
| `getSendSMSResult` | Read | Poll SMS delivery status by `watcherId`. | Not implemented. | Pair with `sendSMS` as async/polling capability. |
| `downloadAndActivateProfile` | Write | eSIM/profile download and activation flow. | Not implemented. | Advanced eSIM capability, not part of core SIM management. |
| `auditSwapProfile` | Read | Audit profile swap. | Not implemented. | Advanced eSIM/audit capability, likely Kite-specific. |
| `networkReset` | Write | Cancel location / reset network registration for 2G/3G, 4G, or both. | Implemented as `KiteAdapter.purge`. | Canonical `POST /v1/sims/{iccid}/purge`. Note: this does not change Kite `lifeCycleStatus`. |

### `modifySubscription` Should Be Decomposed

`modifySubscription` is large and should not become a single public "do anything" endpoint. The WSDL allows several categories:

| Native field group | Possible product capability | Cross-provider notes |
|---|---|---|
| `lifeCycleStatus`, `forceRetired` | `set_administrative_status` | Already partially implemented. Kite documented write subset maps to `inactive_new`, `in_test`, `activation_ready`, `activation_pendant`, `active`. Other states must be rejected unless confirmed. |
| `alias`, `customField1-4` | `alias_custom_fields` or `sim_metadata_write` | Tele2 can edit custom fields; Moabits has name/update features. Good candidate for a future canonical patch. |
| `commercialGroup`, `supervisionGroup` | `plan_or_group_management` | Needs plan/group catalog validation. Do not expose until catalogs are modeled. |
| `lteEnabled`, `qci`, `voLteEnabled` | `network_feature_management` | Provider-specific/risky. Requires operational confirmation. |
| `apn0-9`, `defaultApn`, `staticIpAddress*`, `additionalStaticIpAddress*` | `apn_ip_management` | High-risk provisioning surface. Keep out of v1 unless product needs it. |
| `dailyConsumptionThreshold`, `monthlyConsumptionThreshold`, `monthlyExpenseLimit` | `quota_management` | Comparable to Moabits limits and partially to Tele2 overage/test limits, but semantics differ. |
| Voice/SMS/data enable flags | `service_toggle_management` | Moabits has data/SMS service toggles; Tele2 mostly controls whole-device status. Needs careful provider matrix. |
| `vpnEnabled`, `advancedSupervisionEnabled`, `locationEnabled` | `feature_flags_management` | Provider-specific. Expose only with explicit UI/ops need. |

## EndCustomer Family

EndCustomer is not part of `/v1/sims`. It represents customer/account management inside Kite.

| Kite operation | Kind | Purpose | Backend guidance |
|---|---|---|---|
| `createEndCustomer` | Write | Create a Kite end customer. | Do not expose through SIM router. Could belong to future provider account administration. |
| `getEndCustomer` | Read | Get one end customer by `endCustomerID`. | Useful for credential/account diagnostics or tenant metadata. |
| `getEndCustomers` | Read | List end customers with pagination. | Possible provider-admin feature, not SIM detail. |
| `modifyEndCustomer` | Write | Modify customer metadata, contacts, addresses, authorization flow fields. | Provider-admin only, requires strong RBAC/audit. |
| `deleteEndCustomer` | Write | Delete customer. | High-risk; avoid unless explicitly required. |
| `deactivateEndCustomer` | Write | Deactivate customer. | High-risk provider-admin operation. |
| `activateEndCustomer` | Write | Activate customer. | High-risk provider-admin operation. |

## User Family

User operations administer remote Kite users, not local application users. Avoid mixing them with the app's identity/RBAC model.

| Kite operation | Kind | Purpose | Backend guidance |
|---|---|---|---|
| `createUser` | Write | Create a Kite user. | Future provider-admin module only. |
| `deleteUser` | Write | Delete a Kite user. | High-risk; require explicit product need. |
| `modifyUser` | Write | Modify Kite user details/roles/status. | Provider-admin module only. |
| `getUsers` | Read | List/search Kite users. | Provider-admin module only. |
| `blockUser` | Write | Block Kite user. | Provider-admin module only. |
| `unblockUser` | Write | Unblock Kite user. | Provider-admin module only. |
| `getRoles` | Read | List Kite roles. | Required if remote user admin is ever implemented. |
| `resetPassword` | Write | Reset Kite user password. | Very sensitive; keep out unless required. |

## Reports Family

Reports are asynchronous. They should be modeled as jobs/artifacts, not as synchronous SIM detail reads.

| Kite operation | Kind | Purpose | Backend guidance |
|---|---|---|---|
| `createDownloadReport` | Write | Request report generation. | Future `ReportProvider`/jobs capability. |
| `getDownloadReportList` | Read | List reports created by the API consumer certificate. | Future report list/status route. |
| `getDownloadReportLink` | Read | Get download URL for a generated report. | Future report download route. |

Cross-provider note: Tele2 has Dynamic Reporting APIs such as aggregated usage and report creation; Moabits has some aggregate usage endpoints. Treat reporting as a separate capability from per-SIM `usage`.

## Echo and Common Files

Echo:

| Operation | Purpose | Backend guidance |
|---|---|---|
| `echo` | Check SOAP service reachability/auth path. | Could support a credential smoke test, not a SIM feature. |

Common files:

| File group | Purpose | Backend guidance |
|---|---|---|
| `UNICA_API_SOAP_common_faults_v1_1.wsdl` | SOAP `ClientException` / `ServerException`. | Use for error mapping to canonical `DomainError` types. |
| `UNICA_API_SOAP_common_types_*` | Shared identifiers, contact/address/date/simple types. | Use for request builders and DTO validation if generated tooling is adopted. |
| `UNICA_API_SOAP_Globalm2m_types_*` | Shared GlobalM2M business types. | Use as schema reference for mappers. |

## Current Backend Mapping

| Canonical backend capability | Kite native operation | Current status |
|---|---|---|
| `list_subscriptions` | `getSubscriptions` | Implemented. |
| `get_subscription` | `getSubscriptionDetail` | Implemented. |
| `get_usage` | `getSubscriptions` / consumption blocks | Implemented for current consumption. Historical windows rejected. |
| `get_presence` | `getPresenceDetail` | Implemented. |
| `set_administrative_status` | `modifySubscription.lifeCycleStatus` | Implemented for documented subset. Feature-flag gated. |
| `purge` | `networkReset` | Implemented. Feature-flag gated. |
| `status_history` | `getStatusHistory` | Adapter implemented, no public route. |
| `location` | `getLocationDetail` | Not implemented. |
| `sms_send` | `sendSMS`, `getSendSMSResult` | Not implemented. |
| `quota_management` | `modifySubscription` thresholds | Not implemented as writes; limits are exposed as read payload when present. |
| `reports` | Reports family | Not implemented. |
| `remote_customer_admin` | EndCustomer family | Not implemented. |
| `remote_user_admin` | User family | Not implemented. |

## Recommended Roadmap

Priority candidates, if the product page needs more actions:

1. **Status history**
   - Add `HistoryProvider` protocol.
   - Expose `GET /v1/sims/{iccid}/status-history`.
   - Kite: `getStatusHistory`.
   - Tele2/Moabits: `not_supported`.

2. **Location**
   - Add `LocationProvider` protocol.
   - Expose `GET /v1/sims/{iccid}/location`.
   - Kite: `getLocationDetail`.
   - Tele2: map to `Get Device Location` if credentials have access.
   - Moabits: confirm documentation before claiming support.

3. **Alias/custom fields**
   - Add a constrained metadata patch, not full `modifySubscription`.
   - Kite: `modifySubscription` alias/custom fields.
   - Tele2: `Edit Device Details` custom fields.
   - Moabits: confirm name/custom field endpoint.

4. **Quota/limit management**
   - Add after validating units and provider behavior.
   - Kite: daily/monthly thresholds and expense limit.
   - Moabits: `setLimits`-style endpoint.
   - Tele2: likely limits in device detail/edit, but semantics differ.

5. **SMS**
   - Add only if the UI needs operational SMS.
   - Model as async/polling because Kite returns `watcherId`.
   - Verify Tele2 SMS add-on and Moabits support before marking broad support.

6. **Reports**
   - Add as a report/job module, not under SIM detail.
   - Useful for reconciliation and bulk export.

7. **Remote Kite customer/user admin**
   - Keep separate from subscriptions.
   - Requires explicit product decision, strict RBAC, and audit.

## Do Not Forget

- Kite certificate/mTLS credentials are tenant-specific and stored encrypted in `company_provider_credentials.credentials_enc`.
- `networkReset` is not the same as a Kite administrative `PURGED` state. The backend maps it to canonical `purge` as a product-level control action.
- Kite usage with historical windows is not supported by the current live adapter path. Historical/bulk usage should go through reports if needed.
- Any new write capability should be feature-flagged, idempotent where possible, and audited similarly to current status/purge writes.
