from enum import StrEnum

from pydantic import BaseModel, Field


class CapabilityStatus(StrEnum):
    SUPPORTED = "supported"
    NOT_SUPPORTED = "not_supported"
    REQUIRES_FEATURE_FLAG = "requires_feature_flag"
    REQUIRES_CONFIRMATION = "requires_confirmation"


class CapabilityOut(BaseModel):
    status: CapabilityStatus
    reason: str | None = None
    targets: list[str] = Field(default_factory=list)


class ProviderCapabilitiesOut(BaseModel):
    provider: str
    capabilities: dict[str, CapabilityOut]
