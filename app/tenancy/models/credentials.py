import uuid as _uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.base import Base


class CompanyProviderCredentials(Base):
    """Encrypted provider credentials per company.

    credentials_enc: Fernet-encrypted JSON blob — never store plaintext here.
                     Kite PFX client certificates belong in this encrypted blob
                     as base64 text plus optional password.
    account_scope:   Non-sensitive metadata visible without decryption
                     (e.g. account_id, environment, kite_account_id,
                     end_customer_id, cert_expires_at).
    Only one active record per (company_id, provider) is allowed —
    enforced by the partial unique index in the DDL migration.
    """

    __tablename__ = "company_provider_credentials"

    id: Mapped[_uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=_uuid.uuid4
    )
    company_id: Mapped[_uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    credentials_enc: Mapped[str] = mapped_column(Text, nullable=False)
    account_scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
