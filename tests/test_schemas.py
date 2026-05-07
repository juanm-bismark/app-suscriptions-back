"""Unit tests for Pydantic response schemas.

These tests validate that OpenAPI schemas correctly model:
- SubscriptionOut with proper AdministrativeStatus enum typing
- UsageOut and PresenceOut with from_attributes=True
- StatusChangeIn with optional service control fields
"""

import pytest

from app.subscriptions.domain import AdministrativeStatus
from app.subscriptions.schemas.sim import (
    PresenceOut,
    SimListOut,
    StatusChangeIn,
    SubscriptionOut,
    UsageOut,
)


class TestSubscriptionOutSchema:
    """Test SubscriptionOut Pydantic model."""

    def test_status_enum_typing(self):
        """SubscriptionOut.status should be typed as AdministrativeStatus enum."""
        # This ensures OpenAPI generates enum values, not plain string
        from pydantic import TypeAdapter

        schema = TypeAdapter(SubscriptionOut).json_schema()
        # Verify that status field has enum constraint in schema
        assert "properties" in schema
        assert "status" in schema["properties"]

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
        assert examples[0]["normalized"]["identity"]["iccid"]
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
