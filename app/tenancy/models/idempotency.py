import uuid as _uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.base import Base


class IdempotencyKey(Base):
    """Processed idempotency keys for mutating SIM operations.

    key:        The Idempotency-Key header value (client-generated UUID).
    response:   Serialised response body — {} for 204 No Content operations.
    company_id: Scopes the key to the issuing company so two companies
                can use the same key value independently.
    expires_at: Safe to purge after this timestamp (default: 24 h).
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("company_id", "key", name="idempotency_keys_company_key_uq"),
    )

    id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid.uuid4
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    company_id: Mapped[_uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
