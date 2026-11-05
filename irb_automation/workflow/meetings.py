"""기관위원회 정기 회의: 안건 자동 구성, 정족수 확인, 이해상충 회피, 표결, 회의록.

정족수·의결 기준은 규칙 세트의 committee 섹션으로 설정한다
(기본값: 재적위원 과반수 출석, 출석위원 과반수 찬성, 사회적·윤리적 위원 출석 필수).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..engine.ruleset import get_ruleset
from ..models import (
    Decision,
    DecisionType,
    Meeting,
    MeetingAttendance,
    MeetingItem,
    Protocol,
    ProtocolStatus,
    ReviewType,
    Role,
    Submission,
    User,
)
from ..notifications import notify
from ..timeutil import now
from . import service as wf

VOTE_KEYS = ("approve", "modify", "defer", "disapprove", "abstain")
VOTE_LABELS = {"approve": "승인", "modify": "시정승인", "defer": "보완", "disapprove": "부결", "abstain": "기권"}
VOTE_TO_DECISION = {
    "approve": DecisionType.APPROVE,
    "modify": DecisionType.APPROVE_WITH_CONDITIONS,
    "defer": DecisionType.DEFER,
    "disapprove": DecisionType.DISAPPROVE,
}


def committee_members(db: Session) -> list[User]:
    return list(db.scalars(
        select(User).where(User.is_committee_member.is_(True), User.is_active.is_(True)).order_by(User.full_name)
    ))


# ------------------------------------------------------------------ 위원회 구성 요건
def composition_check(db: Session) -> dict[str, Any]:
    policy = get_ruleset().data.get("committee", {})
    members = committee_members(db)
    t = date.today()
    checks = []

    def add(label: str, ok: bool, detail: str) -> None:
        checks.append({"label": label, "ok": ok, "detail": detail})

    n = len(members)
    add("위원 수", n >= int(policy.get("min_members", 5)), f"{n}명 (최소 {policy.get('min_members', 5)}명)")
    chairs = [m for m in members if m.role == Role.CHAIR]
    add("위원장", len(chairs) == 1, f"{len(chairs)}명")
    if policy.get("require_both_genders", True):
        genders = {m.gender for m in members if m.gender}
        add("성별 구성(하나의 성으로만 구성 불가)", {"M", "F"} <= genders,
            f"남 {sum(m.gender == 'M' for m in members)} · 여 {sum(m.gender == 'F' for m in members)} · 미입력 {sum(not m.gender for m in members)}")
    lay = [m for m in members if m.is_lay_member]
    add("사회적·윤리적 타당성 평가 위원", len(lay) >= int(policy.get("min_lay_members", 1)), ", ".join(m.full_name for m in lay) or "없음")
    ext = [m for m in members if m.is_external_member]
    add("기관 비종사 위원", len(ext) >= int(policy.get("min_external_members", 1)), ", ".join(m.full_name for m in ext) or "없음")
    expired_term = [m.full_name for m in members if m.term_end and m.term_end < t]
    add("임기 유효", not expired_term, ", ".join(expired_term) or "모두 유효")
    expired_training = [m.full_name for m in members if not m.training_expires_on or m.training_expires_on < t]
    add("위원 교육 이수", not expired_training, ", ".join(expired_training) or "모두 유효")
    return {"members": members, "checks": checks, "ok": all(c["ok"] for c in checks),
            "legal": get_ruleset().legal("bioethics_11")}


# ------------------------------------------------------------------ 회의
def create_meeting(db: Session, user: User, scheduled_at: datetime, location: str = "", ip: str = "") -> Meeting:
    wf.require_staff(user)
    year = scheduled_at.year
    count = db.scalar(select(func.count(Meeting.id)).where(Meeting.meeting_no.like(f"{year}-%"))) or 0
    m = Meeting(meeting_no=f"{year}-{count + 1:02d}", scheduled_at=scheduled_at, location=location, created_by=user.id)
    db.add(m)
    db.flush()
    for member in committee_members(db):
        db.add(MeetingAttendance(meeting_id=m.id, user_id=member.id, present=False))
    db.flush()
    audit.record(db, user, "meeting_create", "meeting", m.id, {"no": m.meeting_no, "at": scheduled_at.isoformat()}, ip)
    notify(db, committee_members(db), f"{m.label} 위원회 개최 예정: {scheduled_at:%Y-%m-%d %H:%M} {location}",
           f"/meetings/{m.id}")
    return m


def _on_agenda(db: Session) -> set[int]:
    return set(db.scalars(select(MeetingItem.submission_id)))


def build_agenda(db: Session, user: User, meeting: Meeting, ip: str = "") -> dict[str, int]:
    """정규심의 대기 신청건을 심의 안건으로, 직전 회의 이후 신속·면제 결정을 보고 안건으로 자동 구성."""
    wf.require_staff(user)
    if meeting.status == "closed":
        raise wf.WorkflowError("종료된 회의입니다.")
    used = _on_agenda(db)
    order = max((i.order for i in meeting.items), default=0)
    added = {"review": 0, "report": 0}

    pending = db.scalars(
        select(Submission).join(Protocol)
        .where(Submission.closed.is_(False), Submission.review_type == ReviewType.FULL,
               Protocol.status.in_([ProtocolStatus.SUBMITTED, ProtocolStatus.UNDER_REVIEW]))
        .order_by(Submission.submitted_at)
    ).all()
    for sub in pending:
        if sub.id in used:
            continue
        order += 1
        db.add(MeetingItem(meeting_id=meeting.id, submission_id=sub.id, item_type="review", order=order,
                           recused_ids=_auto_recusals(sub)))
        added["review"] += 1

    prev = db.scalars(
        select(Meeting).where(Meeting.status == "closed", Meeting.id != meeting.id).order_by(Meeting.scheduled_at.desc())
    ).first()
    since = prev.scheduled_at if prev else meeting.scheduled_at - timedelta(days=365)
    reported = db.scalars(
        select(Decision).where(Decision.decided_at >= since, Decision.review_type != ReviewType.FULL)
        .order_by(Decision.decided_at)
    ).all()
    for d in reported:
        if d.submission_id in used:
            continue
        order += 1
        db.add(MeetingItem(meeting_id=meeting.id, submission_id=d.submission_id, item_type="report",
                           order=order, decision_id=d.id))
        added["report"] += 1
    db.flush()
    db.refresh(meeting)
    audit.record(db, user, "meeting_agenda", "meeting", meeting.id, added, ip)
    return added


def _auto_recusals(sub: Submission) -> list[int]:
    """연구팀에 속한 위원은 자동 회피."""
    return sorted(sub.protocol.member_ids())


def set_attendance(db: Session, user: User, meeting: Meeting, present_ids: set[int], ip: str = "") -> None:
    wf.require_staff(user)
    known = {a.user_id: a for a in meeting.attendance}
    for member in committee_members(db):
        if member.id not in known:
            a = MeetingAttendance(meeting_id=meeting.id, user_id=member.id)
            db.add(a)
            known[member.id] = a
    for uid, a in known.items():
        a.present = uid in present_ids
    if meeting.status == "scheduled" and present_ids:
        meeting.status = "in_progress"
    db.flush()
    audit.record(db, user, "meeting_attendance", "meeting", meeting.id, {"present": sorted(present_ids)}, ip)


def quorum_status(db: Session, meeting: Meeting, item: MeetingItem | None = None) -> dict[str, Any]:
    policy = get_ruleset().data.get("committee", {})
    members = committee_members(db)
    member_ids = {m.id for m in members}
    recused = set(item.recused_ids or []) if item else set()
    present = (meeting.present_ids & member_ids) - recused
    total = len(member_ids)
    need = total // 2 + 1 if policy.get("quorum", "majority_of_members") == "majority_of_members" else 1
    present_members = [m for m in members if m.id in present]
    reasons = []
    if len(present) < need:
        reasons.append(f"출석 {len(present)}명 < 의사정족수 {need}명(재적 {total}명 과반수)")
    if policy.get("require_lay_present", True) and not any(m.is_lay_member for m in present_members):
        reasons.append("사회적·윤리적 타당성 평가 위원 미출석")
    if policy.get("require_external_present", False) and not any(m.is_external_member for m in present_members):
        reasons.append("기관 비종사 위원 미출석")
    return {"ok": not reasons, "present": len(present), "required": need, "total": total,
            "recused": len(recused), "reasons": reasons}


def record_item_decision(
    db: Session,
    user: User,
    item: MeetingItem,
    votes: dict[str, int],
    recused_ids: list[int],
    conditions: str = "",
    comments: str = "",
    discussion: str = "",
    ip: str = "",
) -> Decision:
    """심의 안건 표결 결과를 확정하고 심의 결정(통지서)까지 처리."""
    wf.require_staff(user)
    meeting = item.meeting
    if item.item_type != "review":
        raise wf.WorkflowError("보고 안건은 표결 대상이 아닙니다.")
    if item.decision_id:
        raise wf.WorkflowError("이미 의결된 안건입니다.")
    item.recused_ids = sorted(set(recused_ids) | set(_auto_recusals(item.submission)))
    q = quorum_status(db, meeting, item)
    if not q["ok"]:
        raise wf.WorkflowError("의결 정족수 미충족: " + "; ".join(q["reasons"]))
    votes = {k: max(0, int(votes.get(k, 0) or 0)) for k in VOTE_KEYS}
    cast = sum(votes.values())
    if cast > q["present"]:
        raise wf.WorkflowError(f"투표 수({cast})가 의결 참여 위원 수({q['present']})를 초과합니다.")
    policy = get_ruleset().data.get("committee", {})
    need = q["present"] // 2 + 1 if policy.get("approval", "majority_of_present") == "majority_of_present" else 1
    winner, top = max(((k, votes[k]) for k in VOTE_TO_DECISION), key=lambda kv: kv[1])
    if top < need:
        raise wf.WorkflowError(f"과반 찬성 의견이 없습니다(최다 '{VOTE_LABELS[winner]}' {top}표 < {need}표). 재논의 후 다시 표결하십시오.")
    item.votes = votes
    item.discussion = discussion.strip()
    decision = wf.decide(
        db, user, item.submission.protocol, VOTE_TO_DECISION[winner], conditions, comments,
        meeting.scheduled_at.date(), ip,
    )
    item.decision_id = decision.id
    db.flush()
    audit.record(db, user, "meeting_vote", "meeting", meeting.id,
                 {"item": item.id, "votes": votes, "decision": decision.decision.value}, ip)
    return decision


def close_meeting(db: Session, user: User, meeting: Meeting, notes: str = "", ip: str = "") -> None:
    wf.require_staff(user)
    open_items = [i for i in meeting.items if i.item_type == "review" and not i.decision_id]
    if open_items:
        raise wf.WorkflowError(f"의결되지 않은 심의 안건 {len(open_items)}건이 있습니다. 표결하거나 안건에서 제외하십시오.")
    meeting.status = "closed"
    meeting.closed_at = now()
    meeting.notes = notes.strip()
    audit.record(db, user, "meeting_close", "meeting", meeting.id, {"items": len(meeting.items)}, ip)


def remove_item(db: Session, user: User, item: MeetingItem, ip: str = "") -> None:
    wf.require_staff(user)
    if item.decision_id and item.item_type == "review":
        raise wf.WorkflowError("의결된 안건은 제외할 수 없습니다.")
    meeting_id = item.meeting_id
    db.delete(item)
    audit.record(db, user, "meeting_agenda", "meeting", meeting_id, {"removed_item": item.id}, ip)
