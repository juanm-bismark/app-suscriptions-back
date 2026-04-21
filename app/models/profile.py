import enum
import uuid as _uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AppRole(str, enum.Enum):
    admin = "admin"
    manager = "manager"
    member = "member"


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[_uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    company_id: Mapped[_uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    role: Mapped[AppRole] = mapped_column(
        Enum(AppRole, name="app_role", create_type=False),
        nullable=False,
        default=AppRole.member,
    )
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
