"""기관 양식 관련 모델."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..models import User
from ..timeutil import now

DEFAULT_PROTOCOL_NO_FORMAT = "IRB-{YYYY}-{SEQ:4}"


class InstitutionProfile(Base):
    """기관 프로필(단일 행): 기관명, IRB명, 과제번호 형식, 통지서 하단 문구, 사무국 연락처."""

    __tablename__ = "institution_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    institution_name: Mapped[str] = mapped_column(String(200), default="")
    irb_name: Mapped[str] = mapped_column(String(200), default="")
    protocol_no_format: Mapped[str] = mapped_column(String(64), default=DEFAULT_PROTOCOL_NO_FORMAT)
    letter_footer: Mapped[str] = mapped_column(Text, default="")
    contact: Mapped[str] = mapped_column(Text, default="")
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class FormDefinition(Base):
    """양식 정의(버전 관리). kind = 'protocol'(과제 신청서 추가 항목) | 'online:<doc_type>'(웹 작성 서식).

    kind별 활성 정의는 최대 1개다. 수정은 항상 새 버전으로 저장되어 과거 제출본의 해석이 보존된다.
    """

    __tablename__ = "form_definitions"
    __table_args__ = (UniqueConstraint("kind", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    schema: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(100), default="")  # 가져온 양식 팩 등 출처
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    creator: Mapped[User | None] = relationship()

    @property
    def is_online(self) -> bool:
        return self.kind.startswith("online:")

    @property
    def doc_type(self) -> str:
        return self.kind.split(":", 1)[1] if self.is_online else ""


class OnlineFormResponse(Base):
    """웹 작성 서식 응답. 개인별 서식(per_member)은 연구진 1인당 1행, 과제 단위 서식은 과제당 1행."""

    __tablename__ = "online_form_responses"
    __table_args__ = (UniqueConstraint("protocol_id", "doc_type", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    definition_id: Mapped[int | None] = mapped_column(ForeignKey("form_definitions.id"), nullable=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="draft")  # draft | submitted
    version: Mapped[int] = mapped_column(Integer, default=0)  # 제출 횟수
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    signature_id: Mapped[int | None] = mapped_column(ForeignKey("signatures.id"), nullable=True)
    document_id: Mapped[int | None] = mapped_column(ForeignKey("documents.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    user: Mapped[User] = relationship()
    definition: Mapped[FormDefinition | None] = relationship()
