import uuid
from datetime import datetime

from pydantic import BaseModel


class CompanyOut(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime

    model_config = {"from_attributes": True}


class CompanyUpdate(BaseModel):
    name: str


class CompanySettingsOut(BaseModel):
    company_id: uuid.UUID
    settings: dict
    updated_at: datetime

    model_config = {"from_attributes": True}


class CompanySettingsUpdate(BaseModel):
    settings: dict
