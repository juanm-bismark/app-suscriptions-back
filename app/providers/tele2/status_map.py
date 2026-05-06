"""Tele2 / Cisco Control Center ↔ canonical AdministrativeStatus mapping.

Official Cisco Control Center enum values:
  ACTIVATED         → active
  TEST_READY        → in_test       (SIM in test mode before production activation)
  DEACTIVATED       → terminated
  INVENTORY         → inventory
  PURGED            → purged
  REPLACED          → replaced
  RETIRED           → retired
  ACTIVATION_READY  → activation_ready

"ACTIVE" and "READY" kept as read-only aliases for forward-compat with
non-standard Tele2 deployments — never sent to the provider (write map
uses only the canonical Cisco enum values).
"""

from app.subscriptions.domain import AdministrativeStatus

_TO_CANONICAL: dict[str, AdministrativeStatus] = {
    # Canonical Cisco Control Center values
    "ACTIVATED": AdministrativeStatus.ACTIVE,
    "TEST_READY": AdministrativeStatus.IN_TEST,
    "DEACTIVATED": AdministrativeStatus.TERMINATED,
    "INVENTORY": AdministrativeStatus.INVENTORY,
    "PURGED": AdministrativeStatus.PURGED,
    "REPLACED": AdministrativeStatus.REPLACED,
    "RETIRED": AdministrativeStatus.RETIRED,
    "ACTIVATION_READY": AdministrativeStatus.ACTIVATION_READY,
    # Legacy / alias values — kept for read-path compat only
    "ACTIVE": AdministrativeStatus.ACTIVE,
    "READY": AdministrativeStatus.IN_TEST,
}

_TO_NATIVE: dict[AdministrativeStatus, str] = {
    AdministrativeStatus.ACTIVE: "ACTIVATED",
    AdministrativeStatus.IN_TEST: "TEST_READY",
    AdministrativeStatus.PURGED: "PURGED",
    AdministrativeStatus.TERMINATED: "DEACTIVATED",
    AdministrativeStatus.INVENTORY: "INVENTORY",
    AdministrativeStatus.REPLACED: "REPLACED",
    AdministrativeStatus.RETIRED: "RETIRED",
    AdministrativeStatus.ACTIVATION_READY: "ACTIVATION_READY",
}


def map_status(native: str) -> AdministrativeStatus:
    return _TO_CANONICAL.get((native or "").upper(), AdministrativeStatus.UNKNOWN)


def to_native(status: AdministrativeStatus) -> str | None:
    return _TO_NATIVE.get(status)
