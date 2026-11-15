"""Versioned checklist definitions and human answers, separate from QC results."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from .source_models import SourceDocument

Answer = Literal["yes", "no", "na", "unknown", "pending"]
Owner = Literal["committee", "researcher"]
Direction = Literal["favorable", "unfavorable", "neutral", "na", "unresolved"]


class ChecklistItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    key: str = Field(pattern=r"^[\w-]+\.[\w.-]+$", max_length=100)
    label: str = Field(min_length=1, max_length=2000)
    favorable: Literal["yes", "no", "none"] = "none"
    applicability: str = ""
    interpretation: str = ""
    auto_doc_type: str = ""


class ChecklistDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    source_document_id: int = Field(gt=0)
    owner: Owner
    items: list[ChecklistItem] = Field(min_length=1, max_length=300)
    reviewed: bool = False


class ChecklistDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    protocol_id: int = Field(gt=0)
    form_id: int = Field(gt=0)
    submission_id: int | None = Field(default=None, gt=0)
    item_key: str
    answer: Answer
    evidence_ref: str = Field(default="", max_length=4000)


class ChecklistForm(Base):
    __tablename__ = "checklist_forms"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_document_id: Mapped[int] = mapped_column(ForeignKey("source_documents.id"))
    owner: Mapped[str] = mapped_column(String(20))
    items: Mapped[list[dict[str, str]]] = mapped_column(JSON, default=list)
    verified_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[SourceDocument] = relationship()

    @property
    def verified(self) -> bool:
        return bool(self.source.verified and self.verified_by and self.verified_at)


class ChecklistResponse(Base):
    __tablename__ = "checklist_responses"
    __table_args__ = (UniqueConstraint("protocol_id", "scope", "form_id", "item_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"), index=True)
    submission_id: Mapped[int | None] = mapped_column(ForeignKey("submissions.id"), nullable=True)
    # Non-null scope keeps draft answers unique on SQLite, where NULLs are distinct.
    scope: Mapped[int] = mapped_column(default=0)
    form_id: Mapped[int] = mapped_column(ForeignKey("checklist_forms.id"))
    item_key: Mapped[str] = mapped_column(String(100))
    answer: Mapped[str] = mapped_column(String(10), default="unknown")
    direction: Mapped[str] = mapped_column(String(16), default="unresolved")
    evidence_ref: Mapped[str] = mapped_column(Text, default="")
    auto_candidate: Mapped[str] = mapped_column(String(10), default="unknown")
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
