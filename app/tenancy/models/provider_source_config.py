from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.base import Base


class ProviderSourceConfig(Base):
    """Global non-secret provider source configuration.

    This is intentionally not company-scoped. Use it for provider-wide metadata
    such as Moabits company code selections that should be available to every
    local company using the same source.
    """

    __tablename__ = "provider_source_configs"

    provider: Mapped[str] = mapped_column(String, primary_key=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
