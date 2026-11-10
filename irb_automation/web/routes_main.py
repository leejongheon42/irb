"""로그인, 대시보드, 알림."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import (
    Notification,
    Protocol,
    ProtocolStatus,
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
    soon = t + timedelta(days=60)
    mine = [p for p in db.scalars(select(Protocol).order_by(Protocol.updated_at.desc())) if user.id in p.member_ids()]
    ctx: dict = {
        "my_protocols": mine,
        "drafts": [p for p in mine if p.is_editable],
        "waiting": [p for p in mine if p.status == S.SUBMITTED],
        "revisions": [p for p in mine if p.status == S.REVISION_REQUESTED],
        "expiring": sorted([p for p in mine if p.status == S.APPROVED and p.expires_at and p.expires_at <= soon],
                           key=lambda p: p.expires_at),
        "notifications": db.scalars(
            select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(8)
        ).all(),
    }
    if user.is_admin:
        everyone = db.scalars(select(Protocol)).all()
        ctx["status_counts"] = Counter(p.status for p in everyone)
        ctx["total_protocols"] = len(everyone)
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
