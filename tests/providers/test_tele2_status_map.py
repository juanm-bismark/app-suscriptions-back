"""Unit tests for Tele2 / Cisco Control Center status mapping."""

from app.providers.tele2.status_map import map_status, to_native
from app.subscriptions.domain import AdministrativeStatus


def test_native_to_canonical_all_known_values():
    """All official Cisco Control Center enum values must map to a known canonical status."""
    cisco_natives = [
        "ACTIVATED",
        "TEST_READY",
        "DEACTIVATED",
        "INVENTORY",
        "PURGED",
        "REPLACED",
        "RETIRED",
        "ACTIVATION_READY",
    ]
    for n in cisco_natives:
        assert map_status(n) != AdministrativeStatus.UNKNOWN, f"{n} should not map to UNKNOWN"


def test_legacy_aliases_still_map():
    """ACTIVE and READY are kept as read-path aliases for non-standard deployments."""
    assert map_status("ACTIVE") == AdministrativeStatus.ACTIVE
    assert map_status("READY") == AdministrativeStatus.IN_TEST


def test_test_ready_maps_to_in_test():
    """TEST_READY is the canonical Cisco enum value for test mode."""
    assert map_status("TEST_READY") == AdministrativeStatus.IN_TEST


def test_unknown_input_maps_to_unknown():
    assert map_status("SOMETHING_ELSE") == AdministrativeStatus.UNKNOWN
    assert map_status("") == AdministrativeStatus.UNKNOWN


def test_canonical_to_native_uses_cisco_enum():
    """Write path must use official Cisco enum values, not legacy aliases."""
    assert to_native(AdministrativeStatus.ACTIVE) == "ACTIVATED"
    assert to_native(AdministrativeStatus.IN_TEST) == "TEST_READY"
    assert to_native(AdministrativeStatus.PURGED) == "PURGED"
    assert to_native(AdministrativeStatus.TERMINATED) == "DEACTIVATED"
    assert to_native(AdministrativeStatus.INVENTORY) == "INVENTORY"
    assert to_native(AdministrativeStatus.REPLACED) == "REPLACED"
    assert to_native(AdministrativeStatus.RETIRED) == "RETIRED"
    assert to_native(AdministrativeStatus.ACTIVATION_READY) == "ACTIVATION_READY"


def test_suspended_not_in_write_map():
    """Cisco Control Center has no SUSPENDED state — must not be sent to the provider."""
    assert to_native(AdministrativeStatus.SUSPENDED) is None


def test_case_insensitive_read():
    """map_status normalises to uppercase before lookup."""
    assert map_status("activated") == AdministrativeStatus.ACTIVE
    assert map_status("test_ready") == AdministrativeStatus.IN_TEST
