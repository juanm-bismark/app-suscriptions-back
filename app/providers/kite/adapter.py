"""Kite (Telefónica) provider adapter — SOAP/XML over HTTPS.

Credentials dict keys:
    endpoint (str): SOAP service URL, e.g. "https://m2m.movistar.es/kite/service/subscriptions".
    username/password (str, optional): WS-Security UsernameToken credentials.
        The Kite binding evidence is certificate-first; UsernameToken is emitted
        only when both values are configured for a deployment that requires it.
    client_cert_pfx_b64 (str, optional): base64 PKCS#12/PFX client certificate.
    client_cert_password (str, optional): PFX password.
    company_id (str): Injected by the service layer — stamped on returned Subscriptions.

Listing scope is implicit: the credentials authenticate against a single Kite
account, so `getSubscriptions` already returns only that account's SIMs.
No additional client-side filter is applied.

Provider-specific fields returned in Subscription.provider_fields (Kite block):
    subscription_id, alias, sim_model, sim_type,
    imei, imei_last_change,
    apn, apn_list, default_apn, static_apn_index,
    static_ip, static_ips, additional_ip, additional_static_ips,
    sgsn_ip, ggsn_ip, ip,
    comm_module_manufacturer, comm_module_model,
    rat_type, qci, operator, country,
    lte_enabled, volte_enabled,
    commercial_group, supervision_group,
    service_pack, service_pack_id,
    billing_account, billing_account_name,
    order_number, customer_id, customer_name, customer_currency,
    master_id, master_name,
    service_provider_enabler_id, service_provider_enabler_name,
    service_provider_commercial_id, service_provider_commercial_name,
    end_customer_name, end_customer_id,
    eid, subscription_type, swap_status, block_status,
    custom_field_1..4,
    provision_date, shipping_date,
    last_state_change_date, last_traffic_date, suspension_next_date,
    profile_swap_date, last_blocked_date, last_unblocked_date,
    manual_location {lat, lng}, automatic_location {lat, lng},
    basic_services (nested dict with voice/data/sms home/roaming flags),
    supplementary_services (list of service names),
    gprs_status, ip_status (nested status dicts),
    consumption_daily {voice, sms, data} each with {limit, value, thr_reached, traffic_cut, enabled},
    consumption_monthly (same shape as consumption_daily).
"""

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.providers.adapter_base import BaseAdapter
from app.providers.kite.client import KiteClient
from app.providers.kite.mappers import (
    parse_presence_fields,
    parse_status_detail,
    parse_status_history,
    parse_subscription,
    parse_usage_snapshot,
)
from app.shared.errors import ProviderProtocolError, UnsupportedOperation
from app.subscriptions.domain import (
    AdministrativeStatus,
    ConnectivityPresence,
    StatusDetail,
    StatusHistoryRecord,
    Subscription,
    SubscriptionSearchFilters,
    UsageSnapshot,
)

from .status_map import to_native


def _format_search_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _filter_usage_metrics(
    snapshot: UsageSnapshot, metrics: list[str] | None
) -> UsageSnapshot:
    if not metrics:
        return snapshot
    requested = set(metrics)
    return replace(
        snapshot,
        usage_metrics=[
            metric
            for metric in snapshot.usage_metrics
            if metric.metric_type in requested
            or metric.metric_type.split(".")[-1] in requested
        ],
    )


def _kite_search_parameters(
    filters: SubscriptionSearchFilters | None,
) -> dict[str, str] | None:
    if not filters or not filters.has_filters:
        return None
    params: dict[str, str] = {}
    if filters.status is not None:
        native_status = to_native(filters.status)
        if native_status is None:
            raise UnsupportedOperation(
                detail=f"Kite getSubscriptions does not support status filter '{filters.status}'"
            )
        params["lifeCycleStatus"] = native_status
    if filters.iccid:
        params["icc"] = filters.iccid
    if filters.imsi:
        params["imsi"] = filters.imsi
    if filters.msisdn:
        params["msisdn"] = filters.msisdn
    if filters.modified_since is not None:
        params["startLastStateChangeDate"] = _format_search_dt(filters.modified_since)
    if filters.modified_till is not None:
        params["endLastStateChangeDate"] = _format_search_dt(filters.modified_till)
    for key, value in filters.custom.items():
        normalized = key.replace("_", "").lower()
        if normalized not in {"customfield1", "customfield2", "customfield3", "customfield4"}:
            raise UnsupportedOperation(
                detail=f"Kite getSubscriptions does not support custom filter '{key}'"
            )
        params[f"customField{normalized[-1]}"] = value
    return params


class KiteAdapter(BaseAdapter):
    """Kite SOAP adapter with circuit breaker (ADR-005).

    Stateless — one singleton in the registry.
    Circuit breaker: opens after 5 failures in 30s window, stays open for 30s.
    """

    def __init__(self):
        super().__init__("kite")

    async def get_subscription(
        self, iccid: str, credentials: dict[str, Any]
    ) -> Subscription:
        return await self._call_with_breaker(
            self._get_subscription_impl, iccid, credentials
        )

    async def _get_subscription_impl(
        self, iccid: str, credentials: dict[str, Any]
    ) -> Subscription:
        root = await KiteClient(credentials).get_subscription_detail(iccid)
        el = root.find(".//{*}subscriptionDetailData")
        if el is None:
            raise ProviderProtocolError(
                detail="Missing subscriptionDetailData in Kite response"
            )
        return parse_subscription(el, iccid, credentials.get("company_id", ""))

    async def get_usage(
        self,
        iccid: str,
        credentials: dict[str, Any],
        *,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        metrics: list[str] | None = None,
    ) -> UsageSnapshot:
        return await self._call_with_breaker(
            self._get_usage_impl, iccid, credentials, start_date, end_date, metrics
        )

    async def _get_usage_impl(
        self,
        iccid: str,
        credentials: dict[str, Any],
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        metrics: list[str] | None = None,
    ) -> UsageSnapshot:
        if start_date is not None or end_date is not None:
            raise UnsupportedOperation(
                detail="Kite exposes current consumption in getSubscriptions; historical usage windows require Reports"
            )
        # Use getSubscriptions with searchParameters to request the subscription
        # and parse the first returned subscriptionData for the usage snapshot.
        root = await KiteClient(credentials).get_subscriptions(
            searchParameters={"icc": iccid}, maxBatchSize=1
        )
        el = root.find(".//{*}subscriptionData")
        if el is None:
            # Fallback: some Kite responses may return subscriptionDetailData directly
            detail = root.find(".//{*}subscriptionDetailData")
            if detail is None:
                raise ProviderProtocolError(
                    detail="Missing subscriptionData in Kite response"
                )
            snapshot = parse_usage_snapshot(detail, iccid)
            return _filter_usage_metrics(snapshot, metrics)
        # subscriptionData contains a nested subscriptionDetailData element
        detail = el.find(".//{*}subscriptionDetailData") or el.find(
            ".//{*}subscriptionDetail"
        )
        if detail is None:
            # Some providers return the detail at the same level
            detail = el
        snapshot = parse_usage_snapshot(detail, iccid)
        return _filter_usage_metrics(snapshot, metrics)

    async def get_presence(
        self, iccid: str, credentials: dict[str, Any]
    ) -> ConnectivityPresence:
        return await self._call_with_breaker(
            self._get_presence_impl, iccid, credentials
        )

    async def _get_presence_impl(
        self, iccid: str, credentials: dict[str, Any]
    ) -> ConnectivityPresence:
        root = await KiteClient(credentials).get_presence_detail(iccid)
        el = root.find(".//{*}presenceDetailData")
        if el is None:
            raise ProviderProtocolError(
                detail="Missing presenceDetailData in Kite response"
            )
        return parse_presence_fields(el, iccid)

    async def set_administrative_status(
        self,
        iccid: str,
        credentials: dict[str, Any],
        *,
        target: AdministrativeStatus,
        idempotency_key: str,
        **_: Any,
    ) -> None:
        return await self._call_with_breaker(
            self._set_administrative_status_impl,
            iccid,
            credentials,
            target,
            idempotency_key,
        )

    async def _set_administrative_status_impl(
        self,
        iccid: str,
        credentials: dict[str, Any],
        target: AdministrativeStatus,
        idempotency_key: str,
    ) -> None:
        # Guard write-paths with feature flag
        if not get_settings().lifecycle_writes_enabled:
            raise UnsupportedOperation(
                detail="Lifecycle write operations are disabled by feature flag"
            )

        native = to_native(target)
        if native is None:
            raise UnsupportedOperation(
                detail=(
                    f"Kite does not support transitioning to status '{target}' via API"
                )
            )

        # Perform the modify operation via the KiteClient.
        await KiteClient(credentials).modify_subscription(iccid, native)

    async def purge(
        self, iccid: str, credentials: dict[str, Any], *, idempotency_key: str
    ) -> None:
        """Apply the platform's canonical purge action for Kite.

        Kite does not expose the same administrative PURGED state used by Tele2
        and Moabits in the line-control surface. In this app's provider-neutral
        context, `purge` maps to Kite's documented `networkReset` operation.
        """
        return await self._call_with_breaker(
            self._purge_impl, iccid, credentials, idempotency_key
        )

    async def _purge_impl(
        self, iccid: str, credentials: dict[str, Any], idempotency_key: str
    ) -> None:
        if not get_settings().lifecycle_writes_enabled:
            raise UnsupportedOperation(
                detail="Lifecycle write operations are disabled by feature flag"
            )
        await KiteClient(credentials).network_reset(iccid)

    async def list_subscriptions(
        self,
        credentials: dict[str, Any],
        *,
        cursor: str | None,
        limit: int,
        filters: SubscriptionSearchFilters | None = None,
    ) -> tuple[list[Subscription], str | None]:
        """List subscriptions on the Kite account these credentials authenticate to.

        Native pagination: `startIndex` (offset) + `maxBatchSize` (1..1000).
        Native rate limits apply at the Kite endpoint and surface as 429 →
        `ProviderRateLimited`.
        """
        company_id = credentials.get("company_id", "")
        try:
            start_index = max(int(cursor or "0"), 0)
        except ValueError:
            start_index = 0
        batch_size = min(max(limit, 1), 1000)
        search_parameters = _kite_search_parameters(filters)
        root = await KiteClient(credentials).get_subscriptions(
            start_index=start_index,
            batch_size=batch_size,
            searchParameters=search_parameters,
        )
        items = root.findall(".//{*}subscriptionData")
        subs = [parse_subscription(el, "", company_id) for el in items]
        next_cursor = str(start_index + len(subs)) if len(subs) == batch_size else None
        return subs, next_cursor

    async def get_status_detail(
        self, iccid: str, credentials: dict[str, Any]
    ) -> StatusDetail:
        """Get current status detail for a subscription."""
        return await self._call_with_breaker(
            self._get_status_detail_impl, iccid, credentials
        )

    async def _get_status_detail_impl(
        self, iccid: str, credentials: dict[str, Any]
    ) -> StatusDetail:
        root = await KiteClient(credentials).get_status_detail(iccid)
        el = root.find(".//{*}statusDetailData")
        if el is None:
            raise ProviderProtocolError(
                detail="Missing statusDetailData in Kite response"
            )
        return parse_status_detail(el, iccid)

    async def get_status_history(
        self,
        iccid: str,
        credentials: dict[str, Any],
        *,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[StatusHistoryRecord]:
        """Get status history for a subscription over an optional date range."""
        return await self._call_with_breaker(
            self._get_status_history_impl, iccid, credentials, start_date, end_date
        )

    async def _get_status_history_impl(
        self,
        iccid: str,
        credentials: dict[str, Any],
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[StatusHistoryRecord]:
        root = await KiteClient(credentials).get_status_history(
            iccid, start_date=start_date, end_date=end_date
        )
        records = root.findall(".//{*}statusHistoryData")
        if not records:
            return []
        return parse_status_history(iccid, records)
