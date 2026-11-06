"""지식베이스 모델."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..timeutil import now

KIND_LABELS = {
    "law": "법령",
    "guideline": "지침·가이드라인",
    "sop": "기관 SOP",
    "faq": "FAQ",
    "finding_guide": "검토 의견 가이드",
    "protocol": "과거 과제",
    "decision": "심의 선례",
    "uploaded": "등록 문서",
}

# 정책 지식(누구에게나 공개)과 선례 지식(과제 열람 권한·위원 역할에 따라 공개)
POLICY_KINDS = ("law", "guideline", "sop", "faq", "finding_guide", "uploaded")
PRECEDENT_KINDS = ("protocol", "decision")
UPLOAD_KINDS = ("sop", "guideline", "law", "faq", "uploaded")


class KnowledgeSource(Base):
    """지식 출처(문서 단위). key는 내장·자동 색인 출처의 고유 키(예: builtin:law:bioethics-16, protocol:12)."""

    __tablename__ = "knowledge_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(String(300))
    ref: Mapped[str] = mapped_column(String(300), default="")
    url: Mapped[str] = mapped_column(String(500), default="")
    version: Mapped[str] = mapped_column(String(60), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        back_populates="source", order_by="KnowledgeChunk.seq", cascade="all, delete-orphan"
    )

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    @property
    def is_builtin(self) -> bool:
        return bool(self.key and self.key.startswith("builtin:"))

    @property
    def is_auto(self) -> bool:
        return self.kind in PRECEDENT_KINDS


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    heading: Mapped[str] = mapped_column(String(300), default="")
    text: Mapped[str] = mapped_column(Text)
    length: Mapped[int] = mapped_column(Integer, default=0)

    source: Mapped[KnowledgeSource] = relationship(back_populates="chunks")


class KnowledgeQuery(Base):
    """검색·질의 기록. 개인정보 패턴은 마스킹된 질문만 저장한다."""

    __tablename__ = "knowledge_queries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    question: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(20))  # search | ai | extractive
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
