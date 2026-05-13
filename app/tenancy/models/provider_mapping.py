import uuid as _uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.base import Base


class CompanyProviderMapping(Base):
    """Company-scoped link to a provider-native company/account."""

    __tablename__ = "company_provider_mappings"

    company_id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companies.id", ondelete="CASCADE"),
        primary_key=True,
    )
    provider: Mapped[str] = mapped_column(String, primary_key=True)
    provider_company_code: Mapped[str] = mapped_column(String, nullable=False)
    provider_company_name: Mapped[str | None] = mapped_column(String, nullable=True)
    clie_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
