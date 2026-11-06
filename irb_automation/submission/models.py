"""문서 제출 관련 모델."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..models import Document, User
from ..timeutil import now


class DocumentComment(Base):
    """문서별 검토 의견 스레드(확인·보완 요청·일반 의견·새 버전 제출 기록)."""

    __tablename__ = "document_comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    protocol_id: Mapped[int] = mapped_column(Integer, index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(20), default="comment")  # comment|accept|request_revision|new_version
    body: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    document: Mapped[Document] = relationship()
    author: Mapped[User] = relationship()

    KIND_LABELS = {
        "comment": "의견",
        "accept": "확인",
        "request_revision": "보완 요청",
        "new_version": "새 버전 제출",
    }

    @property
    def kind_label(self) -> str:
        return self.KIND_LABELS.get(self.kind, self.kind)
