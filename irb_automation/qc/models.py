from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
    verdict: Mapped[str] = mapped_column(String(8), default="HOLD")
    counts: Mapped[dict] = mapped_column(JSON, default=dict)
    docs: Mapped[list] = mapped_column(JSON, default=list)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    phases: Mapped[dict] = mapped_column(JSON, default=dict)
    profile: Mapped[dict] = mapped_column(JSON, default=dict)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    ai_used: Mapped[bool] = mapped_column(Boolean, default=False)
    criteria_version: Mapped[str] = mapped_column(String(32), default="")
    outputs: Mapped[list] = mapped_column(JSON, default=list)  # 자동 교정 결과 파일
    status: Mapped[str] = mapped_column(String(12), default="done")
    stage: Mapped[str] = mapped_column(String(80), default="")
    error: Mapped[str] = mapped_column(String(200), default="")
    mvp_attempt: Mapped[str | None] = mapped_column(String(32), default=None)
    stage_updated_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)

    protocol: Mapped[Protocol | None] = relationship()
    creator: Mapped[User] = relationship()

    @property
    def passed(self) -> bool:
        return self.verdict == "PASS"

    def issue(self, iid: int) -> dict | None:
        return next((i for i in self.issues if i["id"] == iid), None)

    def doc(self, key: str) -> dict | None:
        return next((d for d in self.docs if d["key"] == key), None)


class QCRuleFeedback(Base):
    """Snapshot of a rule finding that AI disputed with verified citations."""

    __tablename__ = "qc_rule_feedback"
    __table_args__ = (UniqueConstraint("run_id", "fingerprint"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("qc_runs.id"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    criteria_version: Mapped[str] = mapped_column(String(32))
    rule_code: Mapped[str] = mapped_column(String(100), index=True)
    rule_id: Mapped[str] = mapped_column(String(20), default="")
    doc_type: Mapped[str] = mapped_column(String(48), default="")
    severity: Mapped[str] = mapped_column(String(12))
    title: Mapped[str] = mapped_column(String(250))
    outcome: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[dict[str, str]]] = mapped_column(JSON)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_note: Mapped[str] = mapped_column(Text, default="")

    run: Mapped[QCRun] = relationship()
    reviewer: Mapped[User | None] = relationship()


class QCRuleKeyword(Base):
    """A literal alias approved for one condition in one existing criterion."""

    __tablename__ = "qc_rule_keywords"
    __table_args__ = (UniqueConstraint("rule_code", "kind", "pattern", "keyword_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("qc_rule_feedback.id"), index=True)
    rule_code: Mapped[str] = mapped_column(String(100), index=True)
    kind: Mapped[str] = mapped_column(String(8))
    pattern: Mapped[str] = mapped_column(Text)
    keyword: Mapped[str] = mapped_column(String(80))
    keyword_key: Mapped[str] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
