import uuid
from datetime import datetime
from typing import Any, Dict

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
    settings: Dict[str, Any]
    updated_at: datetime

    model_config = {"from_attributes": True}


class CompanySettingsUpdate(BaseModel):
    settings: Dict[str, Any]
