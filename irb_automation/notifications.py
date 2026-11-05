from __future__ import annotations

import logging
from collections.abc import Iterable

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
    if count:
        log.info("notify %d user(s): %s", count, message)
    return count


def staff(db: Session) -> list[User]:
    return list(
        db.scalars(
            select(User).where(User.role.in_([Role.COORDINATOR, Role.CHAIR]), User.is_active.is_(True))
        )
    )
