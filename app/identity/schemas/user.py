from pydantic import BaseModel

from app.identity.models.profile import AppRole


class UserCreate(BaseModel):
    email: str
    password: str
    full_name: str | None = None
    role: AppRole = AppRole.member


class UserUpdate(BaseModel):
    email: str | None = None
    password: str | None = None
    full_name: str | None = None
    role: AppRole | None = None
