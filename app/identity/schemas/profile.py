import uuid
from datetime import datetime

from pydantic import BaseModel, field_validator

from app.identity.models.profile import AppRole


class ProfileOut(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID | str | None
    email: str | None = None
    role: AppRole
    full_name: str | None
    created_at: datetime

    @field_validator("company_id", mode="before")
    @classmethod
    def _stringify_company_id(cls, value: object) -> str | None:
        return str(value) if value is not None else None

    model_config = {"from_attributes": True}


class ProfileUpdate(BaseModel):
    email: str | None = None
    password: str | None = None
    full_name: str | None = None
