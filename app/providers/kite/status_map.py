"""Kite ↔ canonical AdministrativeStatus mapping.

Native Kite lifeCycleStatus values:
  INACTIVE_NEW       → inactive_new
  TEST               → in_test
  ACTIVATION_READY   → activation_ready
  ACTIVATION_PENDANT → activation_pendant
  ACTIVE             → active
  SUSPENDED          → suspended
  DEACTIVATED        → terminated
  RETIRED            → retired
  RESTORE            → restore
  PENDING            → pending

Only the documented modifySubscription subset is sent on write:
INACTIVE_NEW, TEST, ACTIVATION_READY, ACTIVATION_PENDANT, ACTIVE.
"""

from app.subscriptions.domain import AdministrativeStatus

_TO_CANONICAL: dict[str, AdministrativeStatus] = {
    "INACTIVE_NEW": AdministrativeStatus.INACTIVE_NEW,
    "ACTIVE": AdministrativeStatus.ACTIVE,
    "TEST": AdministrativeStatus.IN_TEST,
    "ACTIVATION_READY": AdministrativeStatus.ACTIVATION_READY,
    "ACTIVATION_PENDANT": AdministrativeStatus.ACTIVATION_PENDANT,
    "DEACTIVATED": AdministrativeStatus.TERMINATED,
    "SUSPENDED": AdministrativeStatus.SUSPENDED,
    "RETIRED": AdministrativeStatus.RETIRED,
    "RESTORE": AdministrativeStatus.RESTORE,
    "PENDING": AdministrativeStatus.PENDING,
}

_TO_NATIVE: dict[AdministrativeStatus, str] = {
    AdministrativeStatus.INACTIVE_NEW: "INACTIVE_NEW",
    AdministrativeStatus.ACTIVE: "ACTIVE",
    AdministrativeStatus.IN_TEST: "TEST",
    AdministrativeStatus.ACTIVATION_READY: "ACTIVATION_READY",
    AdministrativeStatus.ACTIVATION_PENDANT: "ACTIVATION_PENDANT",
}


def map_status(native: str) -> AdministrativeStatus:
    return _TO_CANONICAL.get(native.upper(), AdministrativeStatus.UNKNOWN)


def to_native(status: AdministrativeStatus) -> str | None:
    return _TO_NATIVE.get(status)
