from pydantic import BaseModel

from app.identity.models.profile import AppRole


class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str | None = None
    role: AppRole = AppRole.member


class UserUpdate(BaseModel):
    full_name: str | None = None
    role: AppRole | None = None
