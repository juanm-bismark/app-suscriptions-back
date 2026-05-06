from fastapi import APIRouter, Depends

from app.config import Settings, get_settings
from app.providers.base import Provider
from app.providers.schemas import (
    CapabilityOut,
    CapabilityStatus,
    ProviderCapabilitiesOut,
)

router = APIRouter(prefix="/providers", tags=["providers"])


_CAPABILITY_NAMES = (
    "list_subscriptions",
    "get_subscription",
    "get_usage",
    "get_presence",
    "set_administrative_status",
    "purge",
    "status_history",
    "aggregated_usage",
    "plan_catalog",
    "quota_management",
)


def _cap(
    status: CapabilityStatus,
    reason: str | None = None,
    targets: list[str] | None = None,
) -> CapabilityOut:
    return CapabilityOut(status=status, reason=reason, targets=targets or [])


def _write_status(settings: Settings) -> CapabilityStatus:
    if settings.lifecycle_writes_enabled:
        return CapabilityStatus.SUPPORTED
    return CapabilityStatus.REQUIRES_FEATURE_FLAG


def _provider_capabilities(
    provider: Provider, settings: Settings
) -> dict[str, CapabilityOut]:
    write_status = _write_status(settings)
    common = {
        "list_subscriptions": _cap(CapabilityStatus.SUPPORTED),
        "get_subscription": _cap(CapabilityStatus.SUPPORTED),
        "get_usage": _cap(CapabilityStatus.SUPPORTED),
        "get_presence": _cap(CapabilityStatus.SUPPORTED),
        "aggregated_usage": _cap(
            CapabilityStatus.NOT_SUPPORTED,
            "Not exposed by backend v1.",
        ),
        "plan_catalog": _cap(
            CapabilityStatus.NOT_SUPPORTED,
            "Plan/catalog endpoints are not part of backend v1.",
        ),
        "quota_management": _cap(
            CapabilityStatus.NOT_SUPPORTED,
            "Limit management is represented in provider payloads, not as a v1 endpoint.",
        ),
    }
    if provider == Provider.KITE:
        common.update(
            {
                "set_administrative_status": _cap(
                    write_status,
                    "Kite modifySubscription supports only the documented lifecycle subset.",
                    [
                        "active",
                        "in_test",
                        "activation_ready",
                        "activation_pendant",
                        "inactive_new",
                    ],
                ),
                "purge": _cap(
                    write_status,
                    "Canonical purge maps to Kite networkReset and does not change lifeCycleStatus.",
                ),
                "status_history": _cap(CapabilityStatus.SUPPORTED),
            }
        )
    elif provider == Provider.TELE2:
        common.update(
            {
                "set_administrative_status": _cap(
                    write_status,
                    "Tele2 lifecycle writes use Edit Device Details with Cisco status values.",
                    [
                        "active",
                        "in_test",
                        "activation_ready",
                        "terminated",
                        "inventory",
                        "purged",
                        "retired",
                        "replaced",
                    ],
                ),
                "purge": _cap(
                    write_status,
                    "Canonical purge maps to Edit Device Details {status: PURGED}.",
                    ["purged"],
                ),
                "status_history": _cap(
                    CapabilityStatus.NOT_SUPPORTED,
                    "Tele2 catalog does not expose a status history endpoint.",
                ),
            }
        )
    else:
        common.update(
            {
                "set_administrative_status": _cap(
                    write_status,
                    "Orion API 2.0.0 exposes active and suspend write routes; TEST_READY, DEACTIVATED, and INVENTORY are not public write targets.",
                    ["active", "suspended"],
                ),
                "purge": _cap(
                    write_status,
                    "Canonical purge maps to Orion API 2.0.0 PUT /api/sim/purge/ with body {iccidList:[...]} and expects info.purged=true.",
                    ["purged"],
                ),
                "status_history": _cap(
                    CapabilityStatus.NOT_SUPPORTED,
                    "moabits.md does not define a status history endpoint.",
                ),
                "aggregated_usage": _cap(
                    CapabilityStatus.NOT_SUPPORTED,
                    "Orion API 2.0.0 exposes GET /api/usage/companyUsage, but backend v1 does not expose aggregated usage.",
                ),
                "plan_catalog": _cap(
                    CapabilityStatus.NOT_SUPPORTED,
                    "Orion service status is consumed inside SIM detail flows; backend v1 does not expose a plan catalog endpoint.",
                ),
                "quota_management": _cap(
                    CapabilityStatus.NOT_SUPPORTED,
                    "Orion API 2.0.0 exposes PUT /api/sim/setLimits/, but backend v1 models limits as SIM payload fields and does not expose quota writes.",
                ),
            }
        )
    return {name: common[name] for name in _CAPABILITY_NAMES}


@router.get("/{provider}/capabilities", response_model=ProviderCapabilitiesOut)
async def get_provider_capabilities(
    provider: Provider,
    settings: Settings = Depends(get_settings),
) -> ProviderCapabilitiesOut:
    return ProviderCapabilitiesOut(
        provider=provider.value,
        capabilities=_provider_capabilities(provider, settings),
    )
