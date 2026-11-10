"""웹 공통 의존성: 템플릿, 현재 사용자, CSRF, 플래시 메시지."""
from __future__ import annotations

import json
import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..audit import ACTION_LABELS
from ..config import get_settings
from ..database import get_db
from ..engine.ruleset import get_ruleset
from ..forms import views as form_views
from ..forms.profile import SettingsView, profile_values
from ..models import (
    ROLE_LABELS,
    STATUS_LABELS,
    Notification,
    ProtocolStatus,
    Role,
    User,
)

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


class LoginRequired(Exception):
    pass


# ------------------------------------------------------------------ 필터/전역
def _fmt_date(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _days_left(value: date | None) -> int | None:
    return None if value is None else (value - date.today()).days


templates.env.filters["fmt"] = _fmt_date
templates.env.filters["days_left"] = _days_left
templates.env.filters["kjson"] = lambda v: json.dumps(v, ensure_ascii=False, default=str)
def _file_version(name: str) -> str:
    from ..qc.facts import version_from_filename

    v = version_from_filename(name or "")
    return f"v{v}" if v else ""


templates.env.filters["file_version"] = _file_version
templates.env.globals["ACTION_LABELS"] = ACTION_LABELS
templates.env.globals["STATUS_LABELS"] = STATUS_LABELS
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS
templates.env.globals["Role"] = Role
templates.env.globals["S"] = ProtocolStatus
# 기관 양식(forms 패키지)
templates.env.globals["institution_protocol_sections"] = form_views.protocol_sections
templates.env.globals["institution_online_progress"] = form_views.online_progress
templates.env.globals["field_show_when"] = form_views.field_show_when
templates.env.globals["field_display"] = form_views.field_display
templates.env.globals["online_status_label"] = form_views.online_status_label


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


# ------------------------------------------------------------------ 세션/CSRF
def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def verify_csrf(request: Request) -> None:
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return
    form = await request.form()
    sent = form.get("csrf_token") or request.headers.get("x-csrf-token")
    expected = request.session.get("csrf")
    if not expected or not sent or not secrets.compare_digest(str(sent), str(expected)):
        if request.url.path == "/login":
            raise LoginRequired()  # 세션 만료(서버 재시작 등): 로그인 화면을 새로 열어 다시 시도
        raise HTTPException(status_code=403, detail="CSRF 토큰이 유효하지 않습니다. 페이지를 새로고침하십시오.")


def flash(request: Request, message: str, category: str = "info") -> None:
    msgs = request.session.setdefault("flash", [])
    msgs.append({"message": message, "category": category})
    request.session["flash"] = msgs


def pop_flashes(request: Request) -> list[dict[str, str]]:
    return request.session.pop("flash", [])


# ------------------------------------------------------------------ 인증
def current_user_optional(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.get(User, uid)
    if user is None or not user.is_active:
        request.session.clear()
        return None
    return user


def current_user(user: User | None = Depends(current_user_optional)) -> User:
    if user is None:
        raise LoginRequired()
    return user


def require_roles(*roles: Role):
    def checker(user: User = Depends(current_user)) -> User:
        if user.role not in roles and user.role != Role.ADMIN:
            raise HTTPException(status_code=403, detail="이 기능에 대한 권한이 없습니다.")
        return user

    return checker


# ------------------------------------------------------------------ 렌더링
def render(request: Request, name: str, db: Session | None = None, user: User | None = None, **ctx: Any):
    # 기관명·IRB명은 기관 프로필(/admin/profile) 값을 우선 사용(템플릿은 그대로 settings.* 사용)
    settings = SettingsView(get_settings(), profile_values(db))
    status = int(ctx.pop("status_code", 200))
    unread = 0
    if db is not None and user is not None:
        unread = db.scalar(
            select(func.count(Notification.id)).where(Notification.user_id == user.id, Notification.is_read.is_(False))
        ) or 0
    token = csrf_token(request)
    context = {
        "user": user,
        "settings": settings,
        "rs": get_ruleset(),
        "unread_count": unread,
        "flashes": pop_flashes(request),
        "csrf_token": token,
        "csrf_input": Markup(f'<input type="hidden" name="csrf_token" value="{token}">'),
        "today": date.today(),
        "page_guide": _page_guide(request.url.path, user),
        **ctx,
    }
    return templates.TemplateResponse(request, name, context, status_code=status)


def _page_guide(path: str, user: User | None):
    from ..help.guides import guide_for

    g = guide_for(path)
    if g is None or (g.admin_only and not (user and user.is_admin)):
        return None
    return g
