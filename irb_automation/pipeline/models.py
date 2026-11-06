from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..timeutil import now


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # document | import | protocol
    protocol_id: Mapped[int | None] = mapped_column(ForeignKey("protocols.id"), nullable=True, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|succeeded|partial|failed
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    input_name: Mapped[str] = mapped_column(String(255), default="")
    input_path: Mapped[str] = mapped_column(String(500), default="")
    input_sha256: Mapped[str] = mapped_column(String(64), default="")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")

    steps: Mapped[list[PipelineStep]] = relationship(
        back_populates="run", order_by="PipelineStep.seq", cascade="all, delete-orphan"
    )

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    def step(self, name: str) -> PipelineStep | None:
        return next((s for s in self.steps if s.name == name), None)


class PipelineStep(Base):
    __tablename__ = "pipeline_steps"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("pipeline_runs.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued|running|succeeded|failed|skipped
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)

    run: Mapped[PipelineRun] = relationship(back_populates="steps")


class ExtractedTextCache(Base):
    """문서 내용(SHA-256)별 추출 텍스트 캐시 - 같은 파일을 다시 인식하지 않는다."""

    __tablename__ = "extracted_texts"

    id: Mapped[int] = mapped_column(primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    method: Mapped[str] = mapped_column(String(30))
    pages: Mapped[int | None] = mapped_column(Integer, nullable=True)
    text: Mapped[str] = mapped_column(Text, default="")
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
