"""기관 서식 템플릿 모델."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..models import User
from ..timeutil import now

MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "hwpx": "application/hwp+zip",
}


class DocumentTemplate(Base):
    """기관이 사용하는 원본 서식 파일과 자리표시자 → 과제 필드 매핑.

    서류 종류(doc_type)마다 활성 서식은 1개이며, 새 버전을 올려도 이전 버전은 이력으로 보존된다.
    """

    __tablename__ = "document_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(50), index=True)
    name: Mapped[str] = mapped_column(String(200))
    fmt: Mapped[str] = mapped_column(String(10))  # docx | hwpx
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # [{"name": "연구제목", "kind": "field|list|compare", "raw": ["{{연구제목}}"], "count": 1}]
    placeholders: Mapped[list] = mapped_column(JSON, default=list)
    # {"연구제목": "title", "작성일": "today|YYYY년 MM월 DD일", "서명": "__keep__"}
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    uploader: Mapped[User] = relationship()

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES[self.fmt]
