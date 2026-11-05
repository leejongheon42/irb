"""로그인, 대시보드, 알림."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (
    DeidJob,
    DestructionRecord,
    Meeting,
    Notification,
    OutputRequest,
    Protocol,
    ProtocolStatus,
    ReviewAssignment,
    Role,
    Submission,
    User,
)
from ..security import throttle, verify_password
from ..timeutil import today
from .deps import (
    client_ip,
    current_user,
    current_user_optional,
    flash,
    render,
    verify_csrf,
)

router = APIRouter()
S = ProtocolStatus


@router.get("/login")
def login_form(request: Request, user: User | None = Depends(current_user_optional)):
    if user:
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(verify_csrf)])
async def login(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    if throttle.is_locked(username):
        flash(request, "로그인 실패가 반복되어 10분간 잠금되었습니다.", "error")
        return RedirectResponse("/login", status_code=303)
    user = db.scalars(select(User).where(User.username == username)).first()
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        throttle.fail(username)
        audit.record(db, None, "login_failed", "user", user.id if user else None, {"username": username}, client_ip(request))
        db.commit()
        flash(request, "아이디 또는 비밀번호가 올바르지 않습니다.", "error")
        return RedirectResponse("/login", status_code=303)
    throttle.reset(username)
    request.session.clear()
    request.session["uid"] = user.id
    audit.record(db, user, "login", "user", user.id, {}, client_ip(request))
    db.commit()
    return RedirectResponse("/", status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    t = today()
    ctx: dict = {}
    soon = t + timedelta(days=60)

    if user.is_irb_staff:
        protocols = db.scalars(select(Protocol).where(Protocol.status != S.DRAFT)).all()
        ctx["status_counts"] = Counter(p.status for p in protocols)
        ctx["queue"] = sorted(
            [p for p in protocols if p.status in (S.SUBMITTED, S.UNDER_REVIEW)],
            key=lambda p: p.current_submission.submitted_at if p.current_submission else p.created_at,
        )
        ctx["expiring"] = sorted(
            [p for p in protocols if p.status == S.APPROVED and p.expires_at and p.expires_at <= soon],
            key=lambda p: p.expires_at,
        )
        ctx["overdue"] = db.scalars(
            select(ReviewAssignment).join(Submission)
            .where(ReviewAssignment.submitted_at.is_(None), ReviewAssignment.due_date < t, Submission.closed.is_(False))
        ).all()
        decided = [s for p in protocols for s in p.submissions if s.decision is not None]
        if decided:
            days = [(s.decision.decided_at - s.submitted_at).days for s in decided]
            ctx["avg_turnaround"] = round(sum(days) / len(days), 1)
        ctx["review_type_counts"] = Counter(p.review_type.label for p in protocols if p.review_type)
        ctx["risk_counts"] = Counter(p.risk_level for p in protocols if p.risk_level)

    if user.role in (Role.REVIEWER, Role.CHAIR, Role.COORDINATOR):
        ctx["my_reviews"] = db.scalars(
            select(ReviewAssignment).join(Submission)
            .where(ReviewAssignment.reviewer_id == user.id, ReviewAssignment.submitted_at.is_(None), Submission.closed.is_(False))
            .order_by(ReviewAssignment.due_date)
        ).all()

    if user.role in (Role.RESEARCHER, Role.ADMIN, Role.COORDINATOR):
        mine = db.scalars(select(Protocol).order_by(Protocol.updated_at.desc())).all()
        ctx["my_protocols"] = [p for p in mine if user.id in p.member_ids()]

    if user.role in (Role.DATA_STEWARD, Role.ADMIN):
        ctx["provision_targets"] = [
            p for p in db.scalars(select(Protocol).where(Protocol.approved_at.is_not(None))).all() if p.is_active_approval
        ]
        ctx["deid_jobs"] = db.scalars(select(DeidJob).order_by(DeidJob.id.desc()).limit(10)).all()

    if user.role == Role.RESEARCHER:
        ctx["released"] = db.scalars(
            select(DeidJob).where(DeidJob.released_to == user.id).order_by(DeidJob.released_at.desc())
        ).all()

    if user.role in (Role.REVIEWER, Role.CHAIR, Role.COORDINATOR, Role.ADMIN):
        ctx["upcoming_meetings"] = db.scalars(
            select(Meeting).where(Meeting.status != "closed").order_by(Meeting.scheduled_at).limit(3)
        ).all()
        ctx["drb_pending"] = db.scalar(
            select(func.count(DeidJob.id)).where(DeidJob.drb_status == "pending", DeidJob.status.in_(["processed", "released"]))
        ) or 0
    if user.role in (Role.DATA_STEWARD, Role.ADMIN):
        ctx["outputs_pending"] = db.scalar(select(func.count(OutputRequest.id)).where(OutputRequest.status == "submitted")) or 0
    ctx["destruction_due"] = [
        r for r in db.scalars(select(DestructionRecord).where(DestructionRecord.destroyed_at.is_(None)))
        if (user.id in r.protocol.member_ids() or user.is_irb_staff) and (r.due_date - t).days <= 60
    ]

    ctx["notifications"] = db.scalars(
        select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(8)
    ).all()
    return render(request, "dashboard.html", db, user, **ctx)


@router.get("/notifications")
def notifications(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = db.scalars(
        select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(200)
    ).all()
    return render(request, "notifications.html", db, user, items=items)


@router.post("/notifications/read-all", dependencies=[Depends(verify_csrf)])
def read_all(user: User = Depends(current_user), db: Session = Depends(get_db)):
    for n in db.scalars(select(Notification).where(Notification.user_id == user.id, Notification.is_read.is_(False))):
        n.is_read = True
    db.commit()
    return RedirectResponse("/notifications", status_code=303)


@router.get("/notifications/{nid}")
def open_notification(nid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    n = db.get(Notification, nid)
    if n is None or n.user_id != user.id:
        return RedirectResponse("/notifications", status_code=303)
    n.is_read = True
    db.commit()
    return RedirectResponse(n.link or "/notifications", status_code=303)
