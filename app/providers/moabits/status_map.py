"""Moabits ↔ canonical AdministrativeStatus mapping.

`moabits.md` names Cisco-style values such as ACTIVATED, TEST_READY,
PURGED, INVENTORY, and DEACTIVATED. Historical payloads observed by the
adapter used CamelCase values such as Active, Ready, and Suspended.
The read path accepts both sets; writes remain limited to the observed
active/suspend routes plus canonical purge in the adapter.
"""

from app.shared.errors import UnsupportedOperation
from app.subscriptions.domain import AdministrativeStatus

_TO_CANONICAL: dict[str, AdministrativeStatus] = {
    "active": AdministrativeStatus.ACTIVE,
    "activated": AdministrativeStatus.ACTIVE,
    "ready": AdministrativeStatus.IN_TEST,
    "test_ready": AdministrativeStatus.IN_TEST,
    "suspended": AdministrativeStatus.SUSPENDED,
    "purged": AdministrativeStatus.PURGED,
    "inventory": AdministrativeStatus.INVENTORY,
    "deactivated": AdministrativeStatus.TERMINATED,
    "retired": AdministrativeStatus.RETIRED,
    "replaced": AdministrativeStatus.REPLACED,
}

_TO_NATIVE: dict[AdministrativeStatus, str] = {
    AdministrativeStatus.ACTIVE: "Active",
    AdministrativeStatus.IN_TEST: "Ready",
    AdministrativeStatus.SUSPENDED: "Suspended",
}


def map_status(native: str) -> AdministrativeStatus:
    return _TO_CANONICAL.get(native.lower(), AdministrativeStatus.UNKNOWN)


def to_native(status: AdministrativeStatus) -> str:
    try:
        return _TO_NATIVE[status]
    except KeyError as exc:
        raise UnsupportedOperation(
            detail=f"Moabits does not support transitioning to status '{status}'"
        ) from exc
