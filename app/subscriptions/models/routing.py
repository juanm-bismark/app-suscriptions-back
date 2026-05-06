import uuid as _uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.base import Base


class SimRoutingMap(Base):
    """Maps iccid → provider + company so fetchers never need fan-out.

    This is a routing index, not a data cache — it stores no SIM state.
    Written on first successful provider query; updated on re-assignment.
    """

    __tablename__ = "sim_routing_map"

    iccid: Mapped[str] = mapped_column(String, primary_key=True)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    company_id: Mapped[_uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
