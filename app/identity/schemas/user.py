import uuid

from pydantic import BaseModel

from app.identity.models.profile import AppRole


class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str | None = None
    role: AppRole = AppRole.member
    company_id: uuid.UUID | None = None


class UserUpdate(BaseModel):
    email: str | None = None
    password: str | None = None
    full_name: str | None = None
    role: AppRole | None = None
    company_id: str | None = None
