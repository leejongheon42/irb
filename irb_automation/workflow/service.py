"""IRB 업무 흐름(상태 전이) 서비스.

  작성중 ──제출──▶ 접수 ──배정──▶ 심의중 ──결정──▶ 승인/시정승인 ─┬─▶ 변경작성중 ─제출─▶ ...
    ▲               │  └─면제확인─▶ 심의면제                     ├─▶ 지속심의(접수) ─▶ 승인(기간연장)
    └──보완요청 ◀────┴──────────── 보완 ◀─┘                       ├─▶ 종료보고(접수) ─▶ 종료
                                                                └─▶ 만료(스케줄러) / 중지
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..engine.facts import PROTOCOL_FIELDS, ProtocolFacts, snapshot_protocol
from ..engine.prereview import run_prereview
from ..models import (
    Comment,
    Decision,
    DecisionType,
    PreReview,
    Protocol,
    ProtocolStatus,
    Recommendation,
    ReviewAssignment,
    ReviewType,
    Role,
    Submission,
    SubmissionKind,
    User,
)
from ..notifications import notify, staff
from ..timeutil import now, today

S = ProtocolStatus


class WorkflowError(Exception):
    """업무 규칙 위반."""


class PermissionDenied(WorkflowError):
    pass


# ---------------------------------------------------------------------- 권한
def is_member(user: User, protocol: Protocol) -> bool:
    return user.id in protocol.member_ids()


def is_assigned_reviewer(db: Session, user: User, protocol: Protocol) -> bool:
    return bool(
        db.scalars(
            select(ReviewAssignment.id)
            .join(Submission)
            .where(Submission.protocol_id == protocol.id, ReviewAssignment.reviewer_id == user.id)
        ).first()
    )


def can_view(db: Session, user: User, protocol: Protocol) -> bool:
    if user.is_irb_staff or is_member(user, protocol):
        return True
    if user.role == Role.DATA_STEWARD and protocol.approved_at is not None:
        return True
    return user.role == Role.REVIEWER and is_assigned_reviewer(db, user, protocol)


def require_staff(user: User) -> None:
    if not user.is_irb_staff:
        raise PermissionDenied("IRB 사무국(간사·위원장) 권한이 필요합니다.")


def require_member(user: User, protocol: Protocol) -> None:
    if not is_member(user, protocol):
        raise PermissionDenied("연구책임자 또는 공동연구자만 수행할 수 있습니다.")


# ---------------------------------------------------------------------- 과제
EDITABLE_FIELDS = set(PROTOCOL_FIELDS) | {"co_investigators"}


def create_protocol(db: Session, user: User, data: dict[str, Any], ip: str = "") -> Protocol:
    if user.role not in (Role.RESEARCHER, Role.ADMIN, Role.COORDINATOR):
        raise PermissionDenied("과제 생성 권한이 없습니다.")
    protocol = Protocol(pi_id=user.id, title=data.get("title") or "(제목 없음)")
    _apply(protocol, data)
    db.add(protocol)
    db.flush()
    audit.record(db, user, "protocol_create", "protocol", protocol.id, {"title": protocol.title}, ip)
    return protocol


def update_protocol(db: Session, user: User, protocol: Protocol, data: dict[str, Any], ip: str = "") -> None:
    require_member(user, protocol)
    if not protocol.is_editable:
        raise WorkflowError(f"'{protocol.status.label}' 상태에서는 수정할 수 없습니다.")
    changed = _apply(protocol, data)
    if changed:
        audit.record(db, user, "protocol_update", "protocol", protocol.id, {"fields": sorted(changed)}, ip)


def _apply(protocol: Protocol, data: dict[str, Any]) -> set[str]:
    changed: set[str] = set()
    for key, value in data.items():
        if key not in EDITABLE_FIELDS:
            continue
        if getattr(protocol, key) != value:
            setattr(protocol, key, value)
            changed.add(key)
    return changed


def _restore_snapshot(protocol: Protocol, snapshot: dict[str, Any]) -> None:
    for key in PROTOCOL_FIELDS:
        if key not in snapshot:
            continue
        value = snapshot[key]
        if key in ("study_start", "study_end", "data_period_start", "data_period_end") and value:
            value = date.fromisoformat(value)
        setattr(protocol, key, value)


def _next_protocol_no(db: Session) -> str:
    year = today().year
    prefix = f"IRB-{year}-"
    count = db.scalar(select(func.count(Protocol.id)).where(Protocol.protocol_no.like(f"{prefix}%"))) or 0
    return f"{prefix}{count + 1:04d}"


# ---------------------------------------------------------------------- 사전검토
def build_facts(
    db: Session, protocol: Protocol, kind: str = "initial", report: dict[str, Any] | None = None
) -> ProtocolFacts:
    pi = protocol.pi or db.get(User, protocol.pi_id)
    previous = None
    for sub in reversed(protocol.submissions):
        if sub.review_type and sub.kind != SubmissionKind.CONTINUING:
            previous = sub.review_type.value
            break
    return ProtocolFacts.from_protocol(
        protocol,
        submission_kind=kind,
        previous_review_type=previous,
        uploaded_doc_types={d.doc_type for d in protocol.documents},
        pi_training_expires_on=pi.training_expires_on if pi else None,
        protocol_expires_at=protocol.expires_at,
        report=report or {},
    )


def next_submission_kind(protocol: Protocol) -> SubmissionKind:
    if protocol.status == S.DRAFT:
        return SubmissionKind.INITIAL
    if protocol.status == S.AMENDMENT or (
        protocol.status == S.REVISION_REQUESTED and protocol.approved_at is not None
    ):
        return SubmissionKind.AMENDMENT
    if protocol.status == S.REVISION_REQUESTED:
        return SubmissionKind.RESUBMISSION
    raise WorkflowError(f"'{protocol.status.label}' 상태에서는 제출할 수 없습니다.")


def preview(db: Session, protocol: Protocol, kind: str | None = None, report: dict[str, Any] | None = None) -> dict[str, Any]:
    """저장하지 않고 사전검토 결과만 계산(작성 중 실시간 점검용)."""
    if kind is None:
        try:
            kind = next_submission_kind(protocol).value
        except WorkflowError:
            kind = "initial"
    return run_prereview(build_facts(db, protocol, kind, report))


def _run_and_store_prereview(db: Session, submission: Submission, facts: ProtocolFacts) -> PreReview:
    result = run_prereview(facts)
    pr = PreReview(
        submission=submission,
        review_type=ReviewType(result["review_type"]),
        risk_score=result["risk"]["score"],
        risk_level=result["risk"]["level"],
        result=result,
        ruleset_version=result["ruleset_version"],
    )
    db.add(pr)
    submission.review_type = pr.review_type
    return pr


# ---------------------------------------------------------------------- 제출
def submit(db: Session, user: User, protocol: Protocol, ip: str = "") -> Submission:
    require_member(user, protocol)
    kind = next_submission_kind(protocol)
    facts = build_facts(db, protocol, kind.value)
    result = run_prereview(facts)
    if not result["ready_for_review"]:
        crit = [f["message"] for f in result["findings"] if f["severity"] == "critical"]
        raise WorkflowError("필수 보완 사항이 있어 제출할 수 없습니다: " + " / ".join(crit[:5]))
    return _create_submission(db, user, protocol, kind, facts, ip=ip)


def submit_report(
    db: Session, user: User, protocol: Protocol, kind: SubmissionKind, report: dict[str, Any], ip: str = ""
) -> Submission:
    """지속심의 신청 또는 종료보고."""
    require_member(user, protocol)
    allowed = {
        SubmissionKind.CONTINUING: (S.APPROVED, S.EXPIRED),
        SubmissionKind.FINAL_REPORT: (S.APPROVED, S.EXEMPT, S.EXPIRED, S.SUSPENDED),
    }
    if kind not in allowed:
        raise WorkflowError("지원하지 않는 보고 유형입니다.")
    if protocol.status not in allowed[kind]:
        raise WorkflowError(f"'{protocol.status.label}' 상태에서는 {kind.label}을(를) 제출할 수 없습니다.")
    facts = build_facts(db, protocol, kind.value, report)
    return _create_submission(db, user, protocol, kind, facts, report=report, ip=ip)


def _create_submission(
    db: Session,
    user: User,
    protocol: Protocol,
    kind: SubmissionKind,
    facts: ProtocolFacts,
    report: dict[str, Any] | None = None,
    ip: str = "",
) -> Submission:
    prior_status = protocol.status.value
    if kind == SubmissionKind.AMENDMENT:
        protocol.version += 1
    if not protocol.protocol_no:
        protocol.protocol_no = _next_protocol_no(db)
    sub = Submission(
        protocol=protocol,
        kind=kind,
        version=protocol.version,
        snapshot=snapshot_protocol(protocol),
        report=report or {},
        submitted_by=user.id,
        submitted_at=now(),
        prior_status=_approval_status_before(protocol, prior_status),
    )
    db.add(sub)
    pr = _run_and_store_prereview(db, sub, facts)
    if kind not in (SubmissionKind.CONTINUING, SubmissionKind.FINAL_REPORT):
        protocol.review_type = pr.review_type
        protocol.risk_score = pr.risk_score
        protocol.risk_level = pr.risk_level
    protocol.status = S.SUBMITTED
    db.flush()
    audit.record(
        db, user, "submit", "protocol", protocol.id,
        {"kind": kind.value, "version": protocol.version, "review_type": pr.review_type.value, "risk": pr.risk_score},
        ip,
    )
    notify(
        db,
        staff(db),
        f"[{kind.label}] {protocol.protocol_no} {protocol.title} - 자동분류: {pr.review_type.label}",
        f"/protocols/{protocol.id}",
    )
    return sub


def _approval_status_before(protocol: Protocol, current: str) -> str | None:
    """변경/지속/종료 신청 전, 되돌아갈 승인 상태를 기억한다."""
    if protocol.approved_at is None:
        return None
    if current in (S.APPROVED.value, S.EXEMPT.value, S.EXPIRED.value, S.SUSPENDED.value):
        return current
    # 변경작성중/보완요청 상태에서 제출된 경우 마지막 승인 결정의 종류로 판단한다.
    last_ok = _last_approved_snapshot(protocol, exclude=None)
    if last_ok and last_ok.decision and last_ok.decision.decision == DecisionType.EXEMPT_CONFIRM:
        return S.EXEMPT.value
    return S.APPROVED.value


# ---------------------------------------------------------------------- 사무국
def request_revision(db: Session, user: User, protocol: Protocol, message: str, ip: str = "") -> None:
    require_staff(user)
    sub = protocol.current_submission
    if protocol.status not in (S.SUBMITTED, S.UNDER_REVIEW) or sub is None:
        raise WorkflowError("접수 또는 심의중인 신청건만 보완 요청할 수 있습니다.")
    if sub.kind in (SubmissionKind.CONTINUING, SubmissionKind.FINAL_REPORT):
        raise WorkflowError("지속심의/종료보고는 '보완 후 재심의' 결정으로 처리하십시오.")
    if not message.strip():
        raise WorkflowError("보완 요청 사유를 입력하십시오.")
    protocol.status = S.REVISION_REQUESTED
    sub.closed = True
    db.add(Comment(protocol_id=protocol.id, author_id=user.id, body=f"[보완요청] {message.strip()}"))
    audit.record(db, user, "request_revision", "protocol", protocol.id, {"message": message.strip()}, ip)
    notify(db, protocol.member_ids(), f"{protocol.protocol_no} 보완 요청: {message.strip()[:80]}", f"/protocols/{protocol.id}")


def assign_reviewers(
    db: Session,
    user: User,
    protocol: Protocol,
    reviewer_ids: list[int],
    primary_id: int | None,
    review_type: ReviewType | None = None,
    due_date: date | None = None,
    ip: str = "",
) -> list[ReviewAssignment]:
    require_staff(user)
    sub = protocol.current_submission
    if protocol.status not in (S.SUBMITTED, S.UNDER_REVIEW) or sub is None:
        raise WorkflowError("접수된 신청건에만 심의위원을 배정할 수 있습니다.")
    if not reviewer_ids:
        raise WorkflowError("심의위원을 1명 이상 선택하십시오.")
    members = protocol.member_ids()
    due = due_date or (today() + timedelta(days=get_settings().reviewer_due_days))
    created: list[ReviewAssignment] = []
    existing = {a.reviewer_id for a in sub.assignments}
    for rid in dict.fromkeys(reviewer_ids):
        if rid in members:
            raise WorkflowError("연구책임자·공동연구자는 해당 과제의 심의위원이 될 수 없습니다(이해상충).")
        reviewer = db.get(User, rid)
        if reviewer is None or reviewer.role not in (Role.REVIEWER, Role.CHAIR, Role.COORDINATOR) or not reviewer.is_active:
            raise WorkflowError("유효하지 않은 심의위원입니다.")
        if rid in existing:
            continue
        a = ReviewAssignment(submission=sub, reviewer_id=rid, is_primary=(rid == primary_id), due_date=due)
        db.add(a)
        created.append(a)
    if review_type is not None and review_type != sub.review_type:
        audit.record(db, user, "status_change", "submission", sub.id,
                     {"review_type": [sub.review_type.value if sub.review_type else None, review_type.value]}, ip)
        sub.review_type = review_type
        if sub.kind not in (SubmissionKind.CONTINUING, SubmissionKind.FINAL_REPORT):
            protocol.review_type = review_type
    protocol.status = S.UNDER_REVIEW
    db.flush()
    audit.record(db, user, "assign_reviewers", "protocol", protocol.id,
                 {"reviewers": [a.reviewer_id for a in created], "due": due.isoformat()}, ip)
    notify(db, [a.reviewer_id for a in created],
           f"심의 배정: {protocol.protocol_no} {protocol.title} (기한 {due.isoformat()})",
           f"/protocols/{protocol.id}")
    return created


def submit_review(
    db: Session,
    user: User,
    assignment: ReviewAssignment,
    recommendation: Recommendation,
    comments: str,
    checklist: dict[str, Any] | None = None,
    coi_declared: bool = False,
    ip: str = "",
) -> None:
    if assignment.reviewer_id != user.id:
        raise PermissionDenied("배정된 심의위원만 의견을 제출할 수 있습니다.")
    protocol = assignment.submission.protocol
    if assignment.submission.closed or protocol.status != S.UNDER_REVIEW:
        raise WorkflowError("심의가 진행 중인 신청건이 아닙니다.")
    if coi_declared:
        assignment.coi_declared = True
        assignment.recommendation = None
        assignment.comments = comments
        assignment.submitted_at = now()
        audit.record(db, user, "review_submit", "assignment", assignment.id, {"coi": True}, ip)
        notify(db, staff(db), f"{protocol.protocol_no} 심의위원 이해상충 신고 - 재배정 필요", f"/protocols/{protocol.id}")
        return
    if recommendation != Recommendation.APPROVE and not comments.strip():
        raise WorkflowError("승인 외 의견은 사유를 입력해야 합니다.")
    assignment.recommendation = recommendation
    assignment.comments = comments
    assignment.checklist = checklist or {}
    assignment.submitted_at = now()
    audit.record(db, user, "review_submit", "assignment", assignment.id, {"recommendation": recommendation.value}, ip)
    sub = assignment.submission
    pending = [a for a in sub.assignments if a.submitted_at is None]
    if not pending:
        notify(db, staff(db), f"{protocol.protocol_no} 심의의견 제출 완료 - 결정 대기", f"/protocols/{protocol.id}")


ALLOWED_DECISIONS: dict[SubmissionKind, set[DecisionType]] = {
    SubmissionKind.INITIAL: {
        DecisionType.APPROVE, DecisionType.APPROVE_WITH_CONDITIONS, DecisionType.DEFER,
        DecisionType.DISAPPROVE, DecisionType.EXEMPT_CONFIRM,
    },
    SubmissionKind.RESUBMISSION: {
        DecisionType.APPROVE, DecisionType.APPROVE_WITH_CONDITIONS, DecisionType.DEFER,
        DecisionType.DISAPPROVE, DecisionType.EXEMPT_CONFIRM,
    },
    SubmissionKind.AMENDMENT: {
        DecisionType.APPROVE, DecisionType.APPROVE_WITH_CONDITIONS, DecisionType.DEFER,
        DecisionType.DISAPPROVE, DecisionType.EXEMPT_CONFIRM,
    },
    SubmissionKind.CONTINUING: {
        DecisionType.APPROVE, DecisionType.APPROVE_WITH_CONDITIONS, DecisionType.DEFER, DecisionType.SUSPEND,
    },
    SubmissionKind.FINAL_REPORT: {DecisionType.ACKNOWLEDGE, DecisionType.DEFER},
}


def decide(
    db: Session,
    user: User,
    protocol: Protocol,
    decision: DecisionType,
    conditions: str = "",
    comments: str = "",
    meeting_date: date | None = None,
    ip: str = "",
) -> Decision:
    require_staff(user)
    sub = protocol.current_submission
    if sub is None or sub.closed or protocol.status not in (S.SUBMITTED, S.UNDER_REVIEW):
        raise WorkflowError("결정할 신청건이 없습니다.")
    if decision not in ALLOWED_DECISIONS[sub.kind]:
        raise WorkflowError(f"{sub.kind.label}에는 '{decision.label}' 결정을 내릴 수 없습니다.")
    rtype = sub.review_type or ReviewType.EXPEDITED
    if decision == DecisionType.EXEMPT_CONFIRM and rtype != ReviewType.EXEMPT:
        raise WorkflowError("심의 유형이 '심의면제'인 경우에만 면제 확인할 수 있습니다. 배정 화면에서 유형을 변경하십시오.")
    if rtype == ReviewType.FULL and decision != DecisionType.ACKNOWLEDGE:
        if meeting_date is None:
            raise WorkflowError("정규심의는 위원회 회의일자를 입력해야 합니다.")
        if user.role not in (Role.CHAIR, Role.ADMIN, Role.COORDINATOR):
            raise PermissionDenied("정규심의 결정 권한이 없습니다.")
    if rtype in (ReviewType.EXPEDITED, ReviewType.FULL) and decision in (
        DecisionType.APPROVE, DecisionType.APPROVE_WITH_CONDITIONS
    ):
        reviewed = [a for a in sub.assignments if a.submitted_at and not a.coi_declared]
        if not reviewed:
            raise WorkflowError("심의위원 의견이 1건 이상 제출되어야 승인할 수 있습니다.")
    if decision in (DecisionType.APPROVE_WITH_CONDITIONS, DecisionType.DEFER, DecisionType.DISAPPROVE, DecisionType.SUSPEND):
        if not (conditions.strip() or comments.strip()):
            raise WorkflowError("해당 결정에는 사유 또는 시정 조건을 입력해야 합니다.")

    validity = timedelta(days=get_settings().approval_validity_days)
    t = today()
    start: date | None = None
    end: date | None = None
    prior = S(sub.prior_status) if sub.prior_status else None

    if decision in (DecisionType.APPROVE, DecisionType.APPROVE_WITH_CONDITIONS):
        if sub.kind == SubmissionKind.CONTINUING:
            base = protocol.expires_at if protocol.expires_at and protocol.expires_at >= t else t
            start, end = t, base + validity
        elif sub.kind == SubmissionKind.AMENDMENT and protocol.expires_at and protocol.approved_at and prior == S.APPROVED:
            start, end = t, protocol.expires_at  # 변경승인은 기존 유효기간 유지
        else:
            start, end = t, t + validity
            protocol.approved_at = t
        # 승인 유효기간은 연구 종료 예정일을 넘지 않는다.
        if protocol.study_end and end and protocol.study_end >= t:
            end = min(end, protocol.study_end)
        protocol.expires_at = end
        protocol.status = S.APPROVED
    elif decision == DecisionType.EXEMPT_CONFIRM:
        start, end = t, protocol.study_end
        protocol.approved_at = protocol.approved_at or t
        protocol.expires_at = end
        protocol.status = S.EXEMPT
    elif decision == DecisionType.DEFER:
        if sub.kind in (SubmissionKind.CONTINUING, SubmissionKind.FINAL_REPORT):
            protocol.status = prior or S.APPROVED
            if protocol.status == S.APPROVED and protocol.expires_at and protocol.expires_at < t:
                protocol.status = S.EXPIRED
        else:
            protocol.status = S.REVISION_REQUESTED
    elif decision == DecisionType.DISAPPROVE:
        if sub.kind == SubmissionKind.AMENDMENT and prior is not None:
            last_ok = _last_approved_snapshot(protocol, exclude=sub)
            if last_ok:
                _restore_snapshot(protocol, last_ok.snapshot)
                protocol.version = last_ok.version
            protocol.status = prior
        else:
            protocol.status = S.DISAPPROVED
    elif decision == DecisionType.ACKNOWLEDGE:
        protocol.status = S.CLOSED
    elif decision == DecisionType.SUSPEND:
        protocol.status = S.SUSPENDED

    seq = sum(1 for s in protocol.submissions if s.decision is not None) + 1
    d = Decision(
        submission=sub,
        decision=decision,
        review_type=rtype,
        meeting_date=meeting_date,
        conditions=conditions.strip(),
        comments=comments.strip(),
        letter_no=f"{protocol.protocol_no}-{seq:02d}",
        decided_by=user.id,
        decided_at=now(),
        approval_start=start,
        approval_end=end,
    )
    db.add(d)
    sub.closed = True
    db.flush()
    audit.record(
        db, user, "decision", "protocol", protocol.id,
        {"decision": decision.value, "kind": sub.kind.value, "letter_no": d.letter_no,
         "approval_end": end.isoformat() if end else None},
        ip,
    )
    notify(db, protocol.member_ids(), f"{protocol.protocol_no} 심의결과: {decision.label}", f"/protocols/{protocol.id}")
    return d


def _last_approved_snapshot(protocol: Protocol, exclude: Submission | None) -> Submission | None:
    for sub in reversed(protocol.submissions):
        if sub is exclude or sub.decision is None:
            continue
        if sub.decision.decision in (
            DecisionType.APPROVE, DecisionType.APPROVE_WITH_CONDITIONS, DecisionType.EXEMPT_CONFIRM
        ) and sub.kind not in (SubmissionKind.CONTINUING, SubmissionKind.FINAL_REPORT):
            return sub
    return None


def start_amendment(db: Session, user: User, protocol: Protocol, ip: str = "") -> None:
    require_member(user, protocol)
    if protocol.status not in (S.APPROVED, S.EXEMPT) or not protocol.is_active_approval:
        raise WorkflowError("유효한 승인 상태에서만 변경 신청을 시작할 수 있습니다.")
    protocol.status = S.AMENDMENT
    audit.record(db, user, "start_amendment", "protocol", protocol.id, {}, ip)


def cancel_amendment(db: Session, user: User, protocol: Protocol, ip: str = "") -> None:
    require_member(user, protocol)
    if protocol.status != S.AMENDMENT:
        raise WorkflowError("변경작성중 상태가 아닙니다.")
    last_ok = _last_approved_snapshot(protocol, exclude=None)
    if last_ok:
        _restore_snapshot(protocol, last_ok.snapshot)
        protocol.status = S.EXEMPT if last_ok.decision.decision == DecisionType.EXEMPT_CONFIRM else S.APPROVED
    else:
        protocol.status = S.APPROVED
    audit.record(db, user, "status_change", "protocol", protocol.id, {"cancel_amendment": True}, ip)


def add_comment(db: Session, user: User, protocol: Protocol, body: str, ip: str = "") -> Comment:
    if not body.strip():
        raise WorkflowError("내용을 입력하십시오.")
    c = Comment(protocol_id=protocol.id, author_id=user.id, body=body.strip())
    db.add(c)
    db.flush()
    audit.record(db, user, "comment", "protocol", protocol.id, {"comment_id": c.id}, ip)
    targets = set(protocol.member_ids()) | {u.id for u in staff(db)}
    targets.discard(user.id)
    notify(db, targets, f"{protocol.protocol_no or protocol.title} 새 의견: {body.strip()[:60]}", f"/protocols/{protocol.id}")
    return c


# ---------------------------------------------------------------------- 스케줄러
def run_scheduler(db: Session) -> dict[str, int]:
    """승인 만료 처리, 만료 예정 알림, 심의 기한 초과 알림. 매일 1회 이상 실행."""
    settings = get_settings()
    t = today()
    stats = {"expired": 0, "expiry_notices": 0, "overdue_notices": 0}

    active = db.scalars(
        select(Protocol).where(Protocol.status.in_([S.APPROVED, S.AMENDMENT]), Protocol.expires_at.is_not(None))
    ).all()
    for p in active:
        assert p.expires_at is not None
        if p.expires_at < t and p.status == S.APPROVED:
            p.status = S.EXPIRED
            stats["expired"] += 1
            audit.record(db, None, "expire", "protocol", p.id, {"expires_at": p.expires_at.isoformat()})
            notify(db, list(p.member_ids()) + staff(db),
                   f"{p.protocol_no} 승인 기간 만료 - 연구를 중단하고 지속심의 또는 종료보고를 제출하십시오.",
                   f"/protocols/{p.id}", dedup_key=f"expired:{p.id}:{p.expires_at}")
            continue
        days_left = (p.expires_at - t).days
        for notice in sorted(settings.expiry_notice_days):
            if 0 <= days_left <= notice:
                stats["expiry_notices"] += notify(
                    db, p.member_ids(),
                    f"{p.protocol_no} 승인 만료 {days_left}일 전({p.expires_at.isoformat()}) - 지속심의를 신청하십시오.",
                    f"/protocols/{p.id}", dedup_key=f"expiry:{p.id}:{p.expires_at}:{notice}",
                )
                break

    overdue = db.scalars(
        select(ReviewAssignment)
        .join(Submission)
        .where(ReviewAssignment.submitted_at.is_(None), ReviewAssignment.due_date < t, Submission.closed.is_(False))
    ).all()
    for a in overdue:
        stats["overdue_notices"] += notify(
            db, [a.reviewer_id],
            f"심의 기한 초과: {a.submission.protocol.protocol_no} (기한 {a.due_date})",
            f"/protocols/{a.submission.protocol_id}", dedup_key=f"overdue:{a.id}:{t}",
        )
    return stats
