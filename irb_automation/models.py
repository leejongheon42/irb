"""데이터 모델.

과제(Protocol) 하나에 여러 심의 신청건(Submission: 신규/재제출/변경/지속/종료)이
누적되며, 각 신청건에는 자동 사전검토 결과(PreReview)와 심의결과(Decision)가 연결된다.
"""
from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base
from .timeutil import now


class Role(str, enum.Enum):
    RESEARCHER = "researcher"
    COORDINATOR = "coordinator"
    REVIEWER = "reviewer"
    CHAIR = "chair"
    DATA_STEWARD = "data_steward"
    ADMIN = "admin"

    @property
    def label(self) -> str:
        return ROLE_LABELS[self]


ROLE_LABELS = {
    Role.RESEARCHER: "연구자",
    Role.COORDINATOR: "IRB 간사",
    Role.REVIEWER: "심의위원",
    Role.CHAIR: "위원장",
    Role.DATA_STEWARD: "데이터관리자",
    Role.ADMIN: "시스템관리자",
}


class ProtocolStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    REVISION_REQUESTED = "revision_requested"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    EXEMPT = "exempt"
    AMENDMENT = "amendment"
    DISAPPROVED = "disapproved"
    SUSPENDED = "suspended"
    EXPIRED = "expired"
    CLOSED = "closed"

    @property
    def label(self) -> str:
        return STATUS_LABELS[self]


STATUS_LABELS = {
    ProtocolStatus.DRAFT: "작성중",
    ProtocolStatus.SUBMITTED: "접수",
    ProtocolStatus.REVISION_REQUESTED: "보완요청",
    ProtocolStatus.UNDER_REVIEW: "심의중",
    ProtocolStatus.APPROVED: "승인",
    ProtocolStatus.EXEMPT: "심의면제",
    ProtocolStatus.AMENDMENT: "변경작성중",
    ProtocolStatus.DISAPPROVED: "부결",
    ProtocolStatus.SUSPENDED: "중지",
    ProtocolStatus.EXPIRED: "만료",
    ProtocolStatus.CLOSED: "종료",
}


class SubmissionKind(str, enum.Enum):
    INITIAL = "initial"
    RESUBMISSION = "resubmission"
    AMENDMENT = "amendment"
    CONTINUING = "continuing"
    FINAL_REPORT = "final_report"

    @property
    def label(self) -> str:
        return {
            "initial": "신규심의",
            "resubmission": "보완 재제출",
            "amendment": "변경심의",
            "continuing": "지속심의",
            "final_report": "종료보고",
        }[self.value]


class ReviewType(str, enum.Enum):
    EXEMPT = "exempt"
    EXPEDITED = "expedited"
    FULL = "full"

    @property
    def label(self) -> str:
        return {"exempt": "심의면제", "expedited": "신속심의", "full": "정규심의"}[self.value]


class DecisionType(str, enum.Enum):
    APPROVE = "approve"
    APPROVE_WITH_CONDITIONS = "approve_with_conditions"
    DEFER = "defer"
    DISAPPROVE = "disapprove"
    EXEMPT_CONFIRM = "exempt_confirm"
    ACKNOWLEDGE = "acknowledge"
    SUSPEND = "suspend"

    @property
    def label(self) -> str:
        return {
            "approve": "승인",
            "approve_with_conditions": "시정승인",
            "defer": "보완 후 재심의",
            "disapprove": "부결",
            "exempt_confirm": "심의면제 확인",
            "acknowledge": "종료보고 확인",
            "suspend": "연구 중지",
        }[self.value]


class Recommendation(str, enum.Enum):
    APPROVE = "approve"
    MODIFY = "modify"
    DEFER = "defer"
    DISAPPROVE = "disapprove"

    @property
    def label(self) -> str:
        return {"approve": "승인", "modify": "시정승인", "defer": "보완", "disapprove": "부결"}[
            self.value
        ]


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    full_name: Mapped[str] = mapped_column(String(100))
    department: Mapped[str] = mapped_column(String(100), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[Role] = mapped_column(Enum(Role, native_enum=False, length=32))
    # 연구윤리(GCP/IRB) 교육 이수 만료일 - 연구자 자격 자동 확인에 사용
    training_expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # 기관위원회 위원 구성 요건(생명윤리법 제11조) 점검용 속성
    is_committee_member: Mapped[bool] = mapped_column(Boolean, default=False)
    is_external_member: Mapped[bool] = mapped_column(Boolean, default=False)  # 기관 비종사자
    is_lay_member: Mapped[bool] = mapped_column(Boolean, default=False)  # 사회적·윤리적 타당성 평가 위원
    gender: Mapped[str] = mapped_column(String(1), default="")  # M | F
    term_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    def has_role(self, *roles: Role) -> bool:
        return self.role in roles

    @property
    def is_irb_staff(self) -> bool:
        return self.role in (Role.COORDINATOR, Role.CHAIR, Role.ADMIN)


class Protocol(Base):
    __tablename__ = "protocols"

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_no: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True)
    title: Mapped[str] = mapped_column(String(300))
    pi_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    co_investigators: Mapped[list] = mapped_column(JSON, default=list)  # [user_id, ...]
    department: Mapped[str] = mapped_column(String(100), default="")
    funding_source: Mapped[str] = mapped_column(String(200), default="")

    # 연구 개요
    study_type: Mapped[str] = mapped_column(String(50), default="retrospective_chart_review")
    objectives: Mapped[str] = mapped_column(Text, default="")
    background: Mapped[str] = mapped_column(Text, default="")
    methods: Mapped[str] = mapped_column(Text, default="")
    target_population: Mapped[str] = mapped_column(Text, default="")
    inclusion_criteria: Mapped[str] = mapped_column(Text, default="")
    exclusion_criteria: Mapped[str] = mapped_column(Text, default="")
    expected_subjects: Mapped[int] = mapped_column(Integer, default=0)
    sample_size_justification: Mapped[str] = mapped_column(Text, default="")
    study_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    study_end: Mapped[date | None] = mapped_column(Date, nullable=True)

    # 데이터 특성
    data_sources: Mapped[list] = mapped_column(JSON, default=list)  # EMR, CDW, PACS, ...
    existing_data_only: Mapped[bool] = mapped_column(Boolean, default=True)
    direct_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    data_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    data_items: Mapped[str] = mapped_column(Text, default="")  # 수집 변수 목록 (줄바꿈 구분)
    identifiers_collected: Mapped[list] = mapped_column(JSON, default=list)
    identifier_justification: Mapped[str] = mapped_column(Text, default="")
    sensitive_categories: Mapped[list] = mapped_column(JSON, default=list)
    vulnerable_populations: Mapped[list] = mapped_column(JSON, default=list)
    unstructured_data: Mapped[list] = mapped_column(JSON, default=list)  # free_text, images, ...
    rare_disease: Mapped[bool] = mapped_column(Boolean, default=False)

    # 개인정보 보호
    deid_level: Mapped[str] = mapped_column(String(30), default="pseudonymized")
    deid_performed_by: Mapped[str] = mapped_column(String(30), default="data_steward")
    storage_location: Mapped[str] = mapped_column(String(50), default="internal_secure")
    access_controls: Mapped[str] = mapped_column(Text, default="")
    external_transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    external_recipient: Mapped[str] = mapped_column(String(200), default="")
    international_transfer: Mapped[bool] = mapped_column(Boolean, default=False)
    data_linkage: Mapped[bool] = mapped_column(Boolean, default=False)
    linkage_target: Mapped[str] = mapped_column(String(200), default="")
    retention_years: Mapped[int] = mapped_column(Integer, default=3)
    destruction_method: Mapped[str] = mapped_column(Text, default="")
    ai_ml_development: Mapped[bool] = mapped_column(Boolean, default=False)
    commercial_use: Mapped[bool] = mapped_column(Boolean, default=False)

    # 다기관 연구 / 심의 방식
    multicenter: Mapped[bool] = mapped_column(Boolean, default=False)
    review_model: Mapped[str] = mapped_column(String(30), default="local")  # local|lead|external
    participating_sites: Mapped[str] = mapped_column(Text, default="")
    external_irb: Mapped[str] = mapped_column(String(200), default="")

    # 기관별 추가 항목(양식 스키마로 정의, forms 패키지)
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)

    # 동의
    consent_plan: Mapped[str] = mapped_column(String(20), default="waiver")  # obtain|waiver
    waiver_impracticable_reasons: Mapped[list] = mapped_column(JSON, default=list)
    waiver_impracticable_detail: Mapped[str] = mapped_column(Text, default="")
    waiver_no_presumed_refusal: Mapped[bool] = mapped_column(Boolean, default=False)
    waiver_minimal_risk_detail: Mapped[str] = mapped_column(Text, default="")

    # 이해상충
    conflict_of_interest: Mapped[bool] = mapped_column(Boolean, default=False)
    coi_detail: Mapped[str] = mapped_column(Text, default="")

    # 상태
    status: Mapped[ProtocolStatus] = mapped_column(
        Enum(ProtocolStatus, native_enum=False, length=32), default=ProtocolStatus.DRAFT, index=True
    )
    review_type: Mapped[ReviewType | None] = mapped_column(
        Enum(ReviewType, native_enum=False, length=16), nullable=True
    )
    risk_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    approved_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_at: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)

    pi: Mapped[User] = relationship(foreign_keys=[pi_id])
    submissions: Mapped[list[Submission]] = relationship(
        back_populates="protocol", order_by="Submission.id", cascade="all, delete-orphan"
    )
    documents: Mapped[list[Document]] = relationship(
        back_populates="protocol", order_by="Document.id", cascade="all, delete-orphan"
    )
    comments: Mapped[list[Comment]] = relationship(
        back_populates="protocol", order_by="Comment.id", cascade="all, delete-orphan"
    )

    @property
    def current_submission(self) -> Submission | None:
        return self.submissions[-1] if self.submissions else None

    @property
    def is_editable(self) -> bool:
        return self.status in (
            ProtocolStatus.DRAFT,
            ProtocolStatus.REVISION_REQUESTED,
            ProtocolStatus.AMENDMENT,
        )

    @property
    def is_active_approval(self) -> bool:
        """현재 유효한 승인(또는 면제 확인)이 있는지. 변경심의 진행 중에도 기존 승인은 유효하다."""
        if self.approved_at is None:
            return False
        if self.status in (
            ProtocolStatus.SUSPENDED,
            ProtocolStatus.EXPIRED,
            ProtocolStatus.CLOSED,
            ProtocolStatus.DISAPPROVED,
        ):
            return False
        return self.expires_at is None or self.expires_at >= date.today()

    def member_ids(self) -> set[int]:
        return {self.pi_id, *[int(x) for x in (self.co_investigators or [])]}


class Submission(Base):
    """심의 신청건. 제출 시점의 과제 내용 스냅샷을 보관한다(버전 관리/감사 추적)."""

    __tablename__ = "submissions"

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"), index=True)
    kind: Mapped[SubmissionKind] = mapped_column(Enum(SubmissionKind, native_enum=False, length=20))
    version: Mapped[int] = mapped_column(Integer, default=1)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    report: Mapped[dict] = mapped_column(JSON, default=dict)  # 지속심의/종료보고 경과 보고 내용
    submitted_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    review_type: Mapped[ReviewType | None] = mapped_column(
        Enum(ReviewType, native_enum=False, length=16), nullable=True
    )
    prior_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)  # revision_requested|decided

    protocol: Mapped[Protocol] = relationship(back_populates="submissions")
    prereviews: Mapped[list[PreReview]] = relationship(
        back_populates="submission", order_by="PreReview.id", cascade="all, delete-orphan"
    )
    assignments: Mapped[list[ReviewAssignment]] = relationship(
        back_populates="submission", order_by="ReviewAssignment.id", cascade="all, delete-orphan"
    )
    decision: Mapped[Decision | None] = relationship(
        back_populates="submission", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def latest_prereview(self) -> PreReview | None:
        return self.prereviews[-1] if self.prereviews else None


class PreReview(Base):
    """규칙 엔진 기반 자동 사전검토 결과."""

    __tablename__ = "prereviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    review_type: Mapped[ReviewType] = mapped_column(Enum(ReviewType, native_enum=False, length=16))
    risk_score: Mapped[int] = mapped_column(Integer)
    risk_level: Mapped[str] = mapped_column(String(16))
    result: Mapped[dict] = mapped_column(JSON)  # 전체 결과(findings, checklist, ...)
    ruleset_version: Mapped[str] = mapped_column(String(64))
    ai_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    submission: Mapped[Submission] = relationship(back_populates="prereviews")


class ReviewAssignment(Base):
    __tablename__ = "review_assignments"
    __table_args__ = (UniqueConstraint("submission_id", "reviewer_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), index=True)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    recommendation: Mapped[Recommendation | None] = mapped_column(
        Enum(Recommendation, native_enum=False, length=16), nullable=True
    )
    comments: Mapped[str] = mapped_column(Text, default="")
    checklist: Mapped[dict] = mapped_column(JSON, default=dict)
    coi_declared: Mapped[bool] = mapped_column(Boolean, default=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    submission: Mapped[Submission] = relationship(back_populates="assignments")
    reviewer: Mapped[User] = relationship()


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"), unique=True)
    decision: Mapped[DecisionType] = mapped_column(Enum(DecisionType, native_enum=False, length=32))
    review_type: Mapped[ReviewType] = mapped_column(Enum(ReviewType, native_enum=False, length=16))
    meeting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    conditions: Mapped[str] = mapped_column(Text, default="")
    comments: Mapped[str] = mapped_column(Text, default="")
    letter_no: Mapped[str] = mapped_column(String(40))
    decided_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    approval_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    approval_end: Mapped[date | None] = mapped_column(Date, nullable=True)

    submission: Mapped[Submission] = relationship(back_populates="decision")
    decider: Mapped[User] = relationship()


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(50))
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    version: Mapped[int] = mapped_column(Integer, default=1)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    protocol: Mapped[Protocol] = relationship(back_populates="documents")


class Comment(Base):
    """IRB-연구자 간 보완 요청/답변 이력."""

    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    protocol: Mapped[Protocol] = relationship(back_populates="comments")
    author: Mapped[User] = relationship()


class DeidJob(Base):
    """가명처리 작업 및 적정성 검토 결과."""

    __tablename__ = "deid_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_id: Mapped[int | None] = mapped_column(ForeignKey("protocols.id"), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    status: Mapped[str] = mapped_column(String(20), default="analyzed")  # analyzed|processed|released
    original_filename: Mapped[str] = mapped_column(String(255))
    input_path: Mapped[str] = mapped_column(String(500))
    input_sha256: Mapped[str] = mapped_column(String(64))
    encoding: Mapped[str] = mapped_column(String(20), default="utf-8")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    report: Mapped[dict] = mapped_column(JSON, default=dict)
    output_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    mapping_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    released_to: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    purpose: Mapped[str] = mapped_column(String(300), default="")  # 처리 목적(처리기록 대장)
    drb_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|approved|rejected
    drb_reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    drb_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    drb_comment: Mapped[str] = mapped_column(Text, default="")

    protocol: Mapped[Protocol | None] = relationship()
    creator: Mapped[User] = relationship(foreign_keys=[created_by])
    drb_reviewer: Mapped[User | None] = relationship(foreign_keys=[drb_reviewer_id])


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    message: Mapped[str] = mapped_column(String(500))
    link: Mapped[str] = mapped_column(String(300), default="")
    dedup_key: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    """감사 추적. 행 단위 해시 체인으로 위변조를 탐지한다."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    row_hash: Mapped[str] = mapped_column(String(64), default="")

    actor: Mapped[User | None] = relationship()


# =====================================================================
# 위원회 회의
# =====================================================================
class Meeting(Base):
    __tablename__ = "meetings"

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_no: Mapped[str] = mapped_column(String(32), unique=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    location: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="scheduled")  # scheduled|in_progress|closed
    notes: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    items: Mapped[list[MeetingItem]] = relationship(
        back_populates="meeting", order_by="MeetingItem.order", cascade="all, delete-orphan"
    )
    attendance: Mapped[list[MeetingAttendance]] = relationship(
        back_populates="meeting", cascade="all, delete-orphan"
    )

    @property
    def present_ids(self) -> set[int]:
        return {a.user_id for a in self.attendance if a.present}

    @property
    def label(self) -> str:
        year, _, seq = self.meeting_no.partition("-")
        return f"{year}년 제{int(seq)}차" if seq.isdigit() else self.meeting_no


class MeetingAttendance(Base):
    __tablename__ = "meeting_attendance"
    __table_args__ = (UniqueConstraint("meeting_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    present: Mapped[bool] = mapped_column(Boolean, default=False)

    meeting: Mapped[Meeting] = relationship(back_populates="attendance")
    user: Mapped[User] = relationship()


class MeetingItem(Base):
    """회의 안건. review=심의 안건(정규심의), report=보고 안건(신속·면제 결과 보고)."""

    __tablename__ = "meeting_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id"), index=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    item_type: Mapped[str] = mapped_column(String(10), default="review")
    order: Mapped[int] = mapped_column(Integer, default=0)
    recused_ids: Mapped[list] = mapped_column(JSON, default=list)
    votes: Mapped[dict] = mapped_column(JSON, default=dict)
    discussion: Mapped[str] = mapped_column(Text, default="")
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("decisions.id"), nullable=True)

    meeting: Mapped[Meeting] = relationship(back_populates="items")
    submission: Mapped[Submission] = relationship()
    decision: Mapped[Decision | None] = relationship()


# =====================================================================
# 전자서명 / 보완 요청 항목
# =====================================================================
class Signature(Base):
    """전자서명 기록: 서명자·의미·대상 내용 해시·시각 (서명 후 내용 변경 여부 검증 가능)."""

    __tablename__ = "signatures"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    meaning: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    signed_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    ip_address: Mapped[str] = mapped_column(String(64), default="")

    user: Mapped[User] = relationship()


class RevisionItem(Base):
    """보완 요청 항목과 연구자의 항목별 답변."""

    __tablename__ = "revision_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"), index=True)
    submission_id: Mapped[int] = mapped_column(ForeignKey("submissions.id"))
    seq: Mapped[int] = mapped_column(Integer, default=1)
    finding_code: Mapped[str] = mapped_column(String(40), default="")
    request_text: Mapped[str] = mapped_column(Text)
    response_text: Mapped[str] = mapped_column(Text, default="")
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open|answered|resolved
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


# =====================================================================
# 데이터 거버넌스
# =====================================================================
class OutputRequest(Base):
    """분석 결과 반출 심사(airlock)."""

    __tablename__ = "output_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"), index=True)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    filename: Mapped[str] = mapped_column(String(255))
    stored_path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    check: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="submitted")  # submitted|approved|approved_suppressed|rejected
    suppressed_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_comment: Mapped[str] = mapped_column(Text, default="")

    protocol: Mapped[Protocol] = relationship()
    requester: Mapped[User] = relationship(foreign_keys=[requested_by])
    reviewer: Mapped[User | None] = relationship(foreign_keys=[reviewer_id])


class Agreement(Base):
    """자료 제공·이용 계약(DUA), 공동연구·결합·위탁 계약."""

    __tablename__ = "agreements"

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_id: Mapped[int | None] = mapped_column(ForeignKey("protocols.id"), nullable=True, index=True)
    agreement_type: Mapped[str] = mapped_column(String(30))
    counterparty: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(300), default="")
    permitted_use: Mapped[str] = mapped_column(Text, default="")
    terms: Mapped[list] = mapped_column(JSON, default=list)
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # draft|active|terminated
    document_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    document_name: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    protocol: Mapped[Protocol | None] = relationship()

    @property
    def is_effective(self) -> bool:
        today_ = date.today()
        if self.status != "active":
            return False
        if self.start_date and self.start_date > today_:
            return False
        return self.end_date is None or self.end_date >= today_


class DestructionRecord(Base):
    """연구 자료 파기 관리."""

    __tablename__ = "destruction_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"), index=True)
    due_date: Mapped[date] = mapped_column(Date, index=True)
    destroyed_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    method: Mapped[str] = mapped_column(Text, default="")
    certified_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    certificate_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    certificate_name: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    protocol: Mapped[Protocol] = relationship()
    certifier: Mapped[User | None] = relationship()


class FeasibilityQuery(Base):
    """IRB 승인 전 대상자 수 사전 조회 기록(차분 공격 감시용)."""

    __tablename__ = "feasibility_queries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    criteria: Mapped[dict] = mapped_column(JSON, default=dict)
    result_display: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)

    user: Mapped[User] = relationship()
