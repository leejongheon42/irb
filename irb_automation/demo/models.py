from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..models import Protocol
from ..timeutil import now


class DemoRun(Base):
    """전체 파이프라인 테스트 실행 기록."""

    __tablename__ = "demo_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|warn|failed
    protocol_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("protocols.id"), nullable=True)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)

    protocol: Mapped[Protocol | None] = relationship()
