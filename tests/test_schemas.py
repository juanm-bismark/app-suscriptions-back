"""Unit tests for Pydantic response schemas.

These tests validate that OpenAPI schemas correctly model:
- SubscriptionOut with raw provider status strings
- UsageOut and PresenceOut with from_attributes=True
- StatusChangeIn with optional service control fields
"""

from app.subscriptions.schemas.sim import (
    PresenceOut,
    ProviderStatusOut,
    SimListOut,
    StatusChangeIn,
    SubscriptionOut,
    UsageOut,
)


class TestSubscriptionOutSchema:
    """Test SubscriptionOut Pydantic model."""

    def test_status_field_is_string(self):
        """SubscriptionOut.status should be a plain string (raw provider value)."""
        from pydantic import TypeAdapter

        schema = TypeAdapter(SubscriptionOut).json_schema()
        assert "properties" in schema
        assert schema["properties"]["status"]["type"] == "string"

    def test_from_attributes_config(self):
        """SubscriptionOut should have from_attributes=True for ORM compatibility."""
        schema = SubscriptionOut.model_config
        assert schema.get("from_attributes") is True

    def test_documents_summary_and_detail_examples(self):
        """SubscriptionOut should document summary and enriched detail shapes."""
        examples = SubscriptionOut.model_config["json_schema_extra"]["examples"]

        assert {example["detail_level"] for example in examples} == {
            "summary",
            "detail",
        }
        assert examples[0]["iccid"]
        assert "iccid" not in examples[0]["normalized"]["identity"]
        assert "value" not in examples[0]["normalized"]["status"]
        assert examples[0]["normalized"]["status"]["group"] == "active_like"
        assert examples[0]["normalized"]["status"]["group_label"] == "Active-like"
        assert examples[0]["normalized"]["status"]["source"] == "provider"
        assert examples[1]["provider_fields"]["detail_enriched"] is True


class TestUsageOutSchema:
    """Test UsageOut Pydantic model."""

    def test_from_attributes_config(self):
        """UsageOut should have from_attributes=True."""
        schema = UsageOut.model_config
        assert schema.get("from_attributes") is True


class TestPresenceOutSchema:
    """Test PresenceOut Pydantic model."""

    def test_from_attributes_config(self):
        """PresenceOut should have from_attributes=True."""
        schema = PresenceOut.model_config
        assert schema.get("from_attributes") is True


class TestStatusChangeInSchema:
    """Test StatusChangeIn request schema."""

    def test_selective_service_control_fields(self):
        """StatusChangeIn should accept optional data_service and sms_service."""
        request = StatusChangeIn(
            target="active",
            data_service=True,
            sms_service=False,
        )

        assert request.target == "active"
        assert request.data_service is True
        assert request.sms_service is False

    def test_optional_service_fields(self):
        """data_service and sms_service should be optional."""
        request = StatusChangeIn(target="suspended")

        assert request.target == "suspended"
        assert request.data_service is None
        assert request.sms_service is None


class TestSimListOutSchema:
    """Test listing metadata for partial provider failures."""

    def test_partial_fields_default_to_successful_listing(self):
        response = SimListOut(items=[], next_cursor=None, total=0)

        assert response.partial is False
        assert response.failed_providers == []
        assert response.provider_statuses == []

    def test_partial_fields_capture_provider_failures(self):
        response = SimListOut(
            items=[],
            next_cursor=None,
            total=1,
            partial=True,
            failed_providers=[
                {
                    "provider": "tele2",
                    "code": "provider.unavailable",
                    "title": "Provider request failed",
                }
            ],
        )

        assert response.partial is True
        assert response.failed_providers[0]["provider"] == "tele2"

    def test_provider_statuses_capture_source_metadata(self):
        response = SimListOut(
            items=[],
            next_cursor=None,
            total=None,
            provider_statuses=[
                ProviderStatusOut(provider="kite", status="ok", count=0),
                ProviderStatusOut(
                    provider="tele2",
                    status="error",
                    code="10000003",
                    title="ModifiedSince is required.",
                ),
                ProviderStatusOut(provider="moabits", status="not_queried"),
            ],
        )

        assert [status.status for status in response.provider_statuses] == [
            "ok",
            "error",
            "not_queried",
        ]
        assert response.provider_statuses[1].code == "10000003"
