from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ..models import Protocol, User
from ..timeutil import now


class QCRun(Base):
    """서류 품질 점검 1회. issues/docs는 JSON(이슈 id는 실행 내 순번)."""

    __tablename__ = "qc_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_id: Mapped[int | None] = mapped_column(ForeignKey("protocols.id"), nullable=True, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    title: Mapped[str] = mapped_column(String(200), default="")
    score: Mapped[int] = mapped_column(Integer, default=100)
    verdict: Mapped[str] = mapped_column(String(8), default="PASS")  # PASS | FAIL
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    docs: Mapped[list] = mapped_column(JSON, default=list)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    phases: Mapped[dict] = mapped_column(JSON, default=dict)
    ai_used: Mapped[bool] = mapped_column(Boolean, default=False)
    criteria_version: Mapped[str] = mapped_column(String(32), default="")
    outputs: Mapped[list] = mapped_column(JSON, default=list)  # 자동 교정 결과 파일

    protocol: Mapped[Protocol | None] = relationship()
    creator: Mapped[User] = relationship()

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    def issue(self, iid: int) -> dict | None:
        return next((i for i in self.issues if i["id"] == iid), None)

    def doc(self, key: str) -> dict | None:
        return next((d for d in self.docs if d["key"] == key), None)
