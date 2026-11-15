"""Versioned source documents and their human verification record."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, CheckConstraint, Date, DateTime, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class SourceDocument(Base):
    """Mutable ORM record for an acquired original or an unverified catalog entry."""

    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint("series", "number", "version", name="uq_source_identity"),
        CheckConstraint("status IN ('current', 'archived', 'unverified')", name="ck_source_status"),
        CheckConstraint(
            "status != 'current' OR (verified_by != '' AND verified_at IS NOT NULL "
            "AND sha256 != '' AND stored_path != '' AND version != '확인 대기')",
            name="ck_source_verified_current",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    series: Mapped[str] = mapped_column(String(50))
    number: Mapped[str] = mapped_column(String(50))
    version: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(255))
    issuer: Mapped[str] = mapped_column(String(255), default="공용기관생명윤리위원회")
    owner: Mapped[str] = mapped_column(String(30), default="researcher")
    sha256: Mapped[str] = mapped_column(String(64), default="")
    stored_path: Mapped[str] = mapped_column(String(500), default="")
    original_filename: Mapped[str] = mapped_column(String(255), default="")
    origin: Mapped[str] = mapped_column(Text, default="")
    notion_block_id: Mapped[str] = mapped_column(String(100), default="")
    acquired_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    reference_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    applies_to: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="unverified", index=True)
    verified_by: Mapped[str] = mapped_column(String(255), default="")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cover_check: Mapped[str] = mapped_column(String(30), default="manual_required")
    verification_note: Mapped[str] = mapped_column(Text, default="")

    @property
    def verified(self) -> bool:
        return bool(
            self.status == "current" and self.verified_by and self.verified_at
            and self.sha256 and self.stored_path and self.version != "확인 대기"
        )
