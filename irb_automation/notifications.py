from __future__ import annotations

import logging
import os
import smtplib
import threading
from collections.abc import Iterable
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Notification, Role, User

log = logging.getLogger(__name__)


def notify(
    db: Session,
    users: Iterable[User | int],
    message: str,
    link: str = "",
    dedup_key: str | None = None,
) -> int:
    """인앱 알림 생성. dedup_key가 같으면 사용자당 1회만 생성(만료 예정 알림 등)."""
    count = 0
    seen: set[int] = set()
    for u in users:
        uid = u if isinstance(u, int) else u.id
        if uid in seen:
            continue
        seen.add(uid)
        if dedup_key and db.scalars(
            select(Notification.id).where(Notification.user_id == uid, Notification.dedup_key == dedup_key)
        ).first():
            continue
        db.add(Notification(user_id=uid, message=message, link=link, dedup_key=dedup_key))
        count += 1
        if smtp_enabled():
            user = u if isinstance(u, User) else db.get(User, uid)
            if user is not None and user.email:
                send_email_async(user.email, message, link)
    if count:
        log.info("notify %d user(s): %s", count, message)
    return count


def smtp_enabled() -> bool:
    return bool(os.environ.get("IRB_SMTP_HOST"))


def _send(to: str, subject: str, body: str) -> None:
    host = os.environ["IRB_SMTP_HOST"]
    port = int(os.environ.get("IRB_SMTP_PORT", "587"))
    msg = EmailMessage()
    msg["From"] = os.environ.get("IRB_SMTP_FROM", "irb-noreply@localhost")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=10) as smtp:
            if os.environ.get("IRB_SMTP_STARTTLS", "true").lower() in ("1", "true", "yes"):
                smtp.starttls()
            user = os.environ.get("IRB_SMTP_USER")
            if user:
                smtp.login(user, os.environ.get("IRB_SMTP_PASSWORD", ""))
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException):
        log.exception("email send failed to %s", to)


def send_email_async(to: str, message: str, link: str = "") -> threading.Thread:
    """메일 발송은 요청 처리를 지연시키지 않도록 백그라운드로 보낸다(실패해도 업무는 계속)."""
    base = os.environ.get("IRB_BASE_URL", "").rstrip("/")
    body = message + (f"\n\n바로가기: {base}{link}" if base and link else "") + "\n\n※ IRB 자동화 시스템 자동 발송 메일입니다."
    t = threading.Thread(target=_send, args=(to, f"[IRB] {message[:60]}", body), daemon=True)
    t.start()
    return t


def staff(db: Session) -> list[User]:
    return list(
        db.scalars(
            select(User).where(User.role.in_([Role.COORDINATOR, Role.CHAIR]), User.is_active.is_(True))
        )
    )
