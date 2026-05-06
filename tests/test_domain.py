"""Unit tests for domain model and status mappings.

These tests validate the canonical domain model:
- AdministrativeStatus enum with all 7 states
- Subscription aggregate with immutability and provider_fields extensibility
- ConnectivityPresence and other value objects
"""

import pytest

from app.subscriptions.domain import (
    AdministrativeStatus,
    ConnectivityPresence,
    ConnectivityState,
    Subscription,
    UsageMetric,
    UsageSnapshot,
)


class TestAdministrativeStatus:
    """Test the canonical AdministrativeStatus enum."""

    def test_all_states_exist(self):
        """All 7 states should be defined."""
        assert AdministrativeStatus.ACTIVE.value == "active"
        assert AdministrativeStatus.IN_TEST.value == "in_test"
        assert AdministrativeStatus.SUSPENDED.value == "suspended"
        assert AdministrativeStatus.TERMINATED.value == "terminated"
        assert AdministrativeStatus.PURGED.value == "purged"
        assert AdministrativeStatus.PENDING.value == "pending"
        assert AdministrativeStatus.UNKNOWN.value == "unknown"

    def test_enum_string_conversion(self):
        """Status enum should convert to/from string."""
        status = AdministrativeStatus("active")
        assert status == AdministrativeStatus.ACTIVE
        assert status.value == "active"


class TestSubscriptionAggregate:
    """Test the Subscription root aggregate."""

    def test_subscription_immutability(self):
        """Subscription should be frozen (immutable)."""
        sub = Subscription(
            iccid="8934070100000000001",
            msisdn="346000000001",
            imsi="214070000000001",
            status=AdministrativeStatus.ACTIVE,
            native_status="ACTIVE",
            provider="moabits",
            company_id="550e8400-0000-0000-0000-000000000001",
            activated_at=None,
            updated_at=None,
        )

        with pytest.raises(AttributeError):
            sub.iccid = "different"  # type: ignore

    def test_subscription_provider_fields_extensible(self):
        """provider_fields should accept any key-value pairs."""
        sub = Subscription(
            iccid="8934070100000000001",
            msisdn=None,
            imsi=None,
            status=AdministrativeStatus.IN_TEST,
            native_status="Ready",
            provider="tele2",
            company_id="550e8400-0000-0000-0000-000000000001",
            activated_at=None,
            updated_at=None,
            provider_fields={
                "rate_plan": "IoT-100",
                "communication_plan": "PLAN-A",
                "custom_tele2_field": "value",
            },
        )

        assert sub.provider_fields["rate_plan"] == "IoT-100"
        assert sub.provider_fields["custom_tele2_field"] == "value"

    def test_subscription_equality_by_value(self):
        """Two Subscriptions with same values should be equal."""
        sub1 = Subscription(
            iccid="8934070100000000001",
            msisdn="346000000001",
            imsi="214070000000001",
            status=AdministrativeStatus.ACTIVE,
            native_status="ACTIVE",
            provider="kite",
            company_id="550e8400-0000-0000-0000-000000000001",
            activated_at=None,
            updated_at=None,
        )

        sub2 = Subscription(
            iccid="8934070100000000001",
            msisdn="346000000001",
            imsi="214070000000001",
            status=AdministrativeStatus.ACTIVE,
            native_status="ACTIVE",
            provider="kite",
            company_id="550e8400-0000-0000-0000-000000000001",
            activated_at=None,
            updated_at=None,
        )

        assert sub1 == sub2


class TestConnectivityPresence:
    """Test the ConnectivityPresence value object."""

    def test_connectivity_states(self):
        """Connectivity should support ONLINE, OFFLINE, UNKNOWN."""
        assert ConnectivityState.ONLINE.value == "online"
        assert ConnectivityState.OFFLINE.value == "offline"
        assert ConnectivityState.UNKNOWN.value == "unknown"

    def test_presence_creation(self):
        """ConnectivityPresence should be creatable with optional fields."""
        presence = ConnectivityPresence(
            iccid="8934070100000000001",
            state=ConnectivityState.ONLINE,
            ip_address="192.168.1.100",
            country_code="ES",
            rat_type="LTE",
            network_name="Movistar",
            last_seen_at=None,
        )

        assert presence.state == ConnectivityState.ONLINE
        assert presence.rat_type == "LTE"


class TestUsageSnapshot:
    """Test the UsageSnapshot value object."""

    def test_usage_snapshot_creation(self):
        """UsageSnapshot should hold period and metrics."""
        from datetime import datetime, timezone
        from decimal import Decimal

        now = datetime.now(tz=timezone.utc)

        usage = UsageSnapshot(
            iccid="8934070100000000001",
            period_start=now.replace(day=1, hour=0, minute=0, second=0, microsecond=0),
            period_end=now,
            data_used_bytes=Decimal("137"),
            sms_count=10,
            voice_seconds=60,
            provider_metrics={"data_mb": 137},
            usage_metrics=[
                UsageMetric(metric_type="data", usage=Decimal("137"), unit="bytes"),
                UsageMetric(metric_type="sms_mo", usage=Decimal("10"), unit="count"),
            ],
        )

        assert usage.data_used_bytes == Decimal("137")
        assert len(usage.usage_metrics) == 2
