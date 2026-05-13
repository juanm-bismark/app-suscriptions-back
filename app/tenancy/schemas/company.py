import uuid
from datetime import datetime
from typing import Any, Dict

from pydantic import AliasChoices, BaseModel, Field


class CompanyOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class CompanyCreate(BaseModel):
    name: str


class CompanyUpdate(BaseModel):
    name: str


class CompanySettingsOut(BaseModel):
    company_id: uuid.UUID
    settings: Dict[str, Any]
    updated_at: datetime

    model_config = {"from_attributes": True}


class CompanySettingsUpdate(BaseModel):
    settings: Dict[str, Any]


class CompanyProviderMappingOut(BaseModel):
    company_id: uuid.UUID
    provider: str
    provider_company_code: str = Field(serialization_alias="companyCode")
    provider_company_name: str | None = Field(
        default=None,
        serialization_alias="companyName",
    )
    clie_id: int | None = None
    settings: Dict[str, Any] = Field(default_factory=dict)
    active: bool
    updated_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class CompanyProviderMappingUpdate(BaseModel):
    provider_company_code: str = Field(
        validation_alias=AliasChoices("companyCode", "provider_company_code"),
        serialization_alias="companyCode",
    )
    provider_company_name: str | None = Field(
        default=None,
        validation_alias=AliasChoices("companyName", "provider_company_name"),
        serialization_alias="companyName",
    )
    clie_id: int | None = None
    settings: Dict[str, Any] = Field(default_factory=dict)


class LocalCompanyProviderMappingOut(BaseModel):
    company_id: uuid.UUID
    company_name: str
    mapping: CompanyProviderMappingOut | None = None


class MoabitsSourceCompanyOut(BaseModel):
    source_company_id: uuid.UUID
    company_code: str = Field(serialization_alias="companyCode")
    company_name: str = Field(serialization_alias="companyName")
    clie_id: int | None = None
    active: bool
    last_seen_at: datetime
    updated_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class MoabitsLinkedCompanyOut(BaseModel):
    company_id: uuid.UUID
    company_name: str


class MoabitsProviderCompanyOut(BaseModel):
    company_code: str = Field(serialization_alias="companyCode")
    company_name: str = Field(serialization_alias="companyName")
    clie_id: int | None = None
    selected_in_source: bool = False
    linked_companies: list[MoabitsLinkedCompanyOut] = Field(default_factory=list)


class MoabitsProviderMappingDiscoveryOut(BaseModel):
    cache_message: str
    source_company_codes: list[str] = Field(default_factory=list)
    local_companies: list[LocalCompanyProviderMappingOut] = Field(default_factory=list)
    moabits_companies: list[MoabitsProviderCompanyOut] = Field(default_factory=list)
