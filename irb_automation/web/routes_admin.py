"""시스템 관리: 사용자, 감사 로그, 규칙 세트, 스케줄러."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..audit import ACTION_LABELS
from ..database import get_db
from ..engine.ruleset import get_ruleset
from ..models import AuditLog, Role, User
from ..security import hash_password, password_problems
from ..timeutil import parse_date
from ..workflow.service import run_scheduler
from .deps import client_ip, flash, render, require_roles, verify_csrf

router = APIRouter(prefix="/admin")
admin_only = require_roles(Role.ADMIN)
staff_only = require_roles(Role.ADMIN, Role.COORDINATOR, Role.CHAIR)


@router.get("/users")
def users(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    items = db.scalars(select(User).order_by(User.role, User.full_name)).all()
    return render(request, "admin/users.html", db, user, items=items, roles=list(Role))


@router.post("/users", dependencies=[Depends(verify_csrf)])
async def create_user(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    problems = password_problems(password)
    if not username or db.scalars(select(User).where(User.username == username)).first():
        flash(request, "아이디가 비어 있거나 이미 사용 중입니다.", "error")
    elif problems:
        flash(request, "비밀번호 정책 미충족: " + ", ".join(problems), "error")
    else:
        u = User(
            username=username,
            password_hash=hash_password(password),
            full_name=str(form.get("full_name", "")).strip() or username,
            department=str(form.get("department", "")).strip(),
            email=str(form.get("email", "")).strip(),
            role=Role(str(form.get("role", "researcher"))),
            training_expires_on=parse_date(str(form.get("training_expires_on", ""))),
        )
        db.add(u)
        db.flush()
        audit.record(db, user, "user_create", "user", u.id, {"username": username, "role": u.role.value}, client_ip(request))
        db.commit()
        flash(request, f"사용자 {username} 생성 완료", "success")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{uid}", dependencies=[Depends(verify_csrf)])
async def update_user(request: Request, uid: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    form = await request.form()
    u = db.get(User, uid)
    if u is None:
        return RedirectResponse("/admin/users", status_code=303)
    changes: dict = {}
    role = form.get("role")
    if role and role != u.role.value:
        changes["role"] = [u.role.value, role]
        u.role = Role(str(role))
    active = form.get("is_active") == "on"
    if active != u.is_active and u.id != user.id:
        changes["is_active"] = active
        u.is_active = active
    training = parse_date(str(form.get("training_expires_on", "")))
    if training != u.training_expires_on:
        changes["training_expires_on"] = str(training)
        u.training_expires_on = training
    new_pw = str(form.get("password", ""))
    if new_pw:
        problems = password_problems(new_pw)
        if problems:
            flash(request, "비밀번호 정책 미충족: " + ", ".join(problems), "error")
            return RedirectResponse("/admin/users", status_code=303)
        u.password_hash = hash_password(new_pw)
        changes["password"] = "reset"
    if changes:
        audit.record(db, user, "user_update", "user", u.id, changes, client_ip(request))
        db.commit()
        flash(request, f"{u.username} 정보가 변경되었습니다.", "success")
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/audit")
def audit_logs(request: Request, action: str = "", user: User = Depends(staff_only), db: Session = Depends(get_db)):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(500)
    if action:
        q = q.where(AuditLog.action == action)
    items = db.scalars(q).all()
    ok, broken = audit.verify_chain(db)
    return render(request, "admin/audit.html", db, user, items=items, chain_ok=ok, broken_id=broken,
                  action=action, labels=ACTION_LABELS)


@router.get("/rules")
def rules(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    rs = get_ruleset()
    text = rs.path.read_text(encoding="utf-8") if rs.path else ""
    return render(request, "admin/rules.html", db, user, ruleset=rs, text=text)


@router.post("/scheduler", dependencies=[Depends(verify_csrf)])
def scheduler(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    stats = run_scheduler(db)
    db.commit()
    flash(request, f"스케줄러 실행: 만료 {stats['expired']}건, 만료예정 알림 {stats['expiry_notices']}건, "
                   f"심의기한 초과 알림 {stats['overdue_notices']}건", "success")
    return RedirectResponse("/", status_code=303)
