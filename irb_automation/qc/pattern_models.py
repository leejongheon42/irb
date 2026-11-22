"""Database tables for imported criteria and editable match expressions."""
from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..timeutil import now


class DocumentRuleSettings(Base):
    __tablename__ = "document_rule_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    criteria: Mapped[dict] = mapped_column(JSON)
    detector_registry: Mapped[dict] = mapped_column(JSON)


class DocumentRulePattern(Base):
    __tablename__ = "document_rule_patterns"
    __table_args__ = (UniqueConstraint("scope", "doc_type", "rule_code", "condition", "target", "pattern_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(16))
    doc_type: Mapped[str] = mapped_column(String(40), index=True)
    rule_code: Mapped[str] = mapped_column(String(100), default="")
    condition: Mapped[str] = mapped_column(String(20), default="")
    target: Mapped[str] = mapped_column(Text, default="")
    expression: Mapped[str] = mapped_column(Text)
    pattern_key: Mapped[str] = mapped_column(String(64))
    format: Mapped[str] = mapped_column(String(16))
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(default=now)


class DocumentRuleChange(Base):
    __tablename__ = "document_rule_changes"
    __table_args__ = (
        CheckConstraint("(pattern_id IS NULL) <> (alias_id IS NULL)", name="ck_document_rule_change_target"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    pattern_id: Mapped[int | None] = mapped_column(ForeignKey("document_rule_patterns.id"), index=True)
    alias_id: Mapped[int | None] = mapped_column(ForeignKey("qc_rule_keywords.id"), index=True)
    restored_change_id: Mapped[int | None] = mapped_column(ForeignKey("document_rule_changes.id"))
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    doc_type: Mapped[str] = mapped_column(String(40), index=True)
    before_active: Mapped[bool | None] = mapped_column(Boolean)
    after_active: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(default=now)
