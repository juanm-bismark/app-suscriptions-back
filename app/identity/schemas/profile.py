import uuid
from datetime import datetime

from pydantic import BaseModel

from app.identity.models.profile import AppRole


class ProfileOut(BaseModel):
    id: uuid.UUID
    company_id: uuid.UUID | None
    email: str | None = None
    role: AppRole
    full_name: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class ProfileUpdate(BaseModel):
    email: str | None = None
    password: str | None = None
    full_name: str | None = None
