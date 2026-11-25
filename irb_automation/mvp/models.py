from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..timeutil import now


class MvpUploadPart(Base):
    __tablename__ = "mvp_upload_parts"

    transfer: Mapped[str] = mapped_column(String(32), primary_key=True)
    index: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(Text)
    size: Mapped[int]
    content: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class MvpUpload(Base):
    __tablename__ = "mvp_uploads"
    __table_args__ = (Index("ix_mvp_uploads_user_run", "user_id", "run_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str] = mapped_column(String(255))
    relative_path: Mapped[str | None] = mapped_column(Text)
    path: Mapped[str] = mapped_column(Text, default="")
    size: Mapped[int]
    fmt: Mapped[str] = mapped_column(String(12))
    doc_type: Mapped[str] = mapped_column(String(40), default="other")
    doc_type_label: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(12))
    message: Mapped[str] = mapped_column(Text, default="")
    run_id: Mapped[int | None] = mapped_column(ForeignKey("qc_runs.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
