"""Read-only service and account usage totals for the highest admin role."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AuditLog, Document, User
from ..mvp.models import MvpUpload
from ..qc.models import QCRuleFeedback, QCRun

PAGE_SIZE = 30


def overview(db: Session, query: str, page: int) -> dict:
    """Aggregate each usage source before joining accounts."""
    ai = AuditLog.action == "ai_transmit"
    activity = select(AuditLog.actor_id.label("uid"), func.max(AuditLog.timestamp).label("last_seen"),
                      func.count().filter(AuditLog.action == "login").label("logins"),
                      func.count().filter(ai).label("ai_calls"),
                      func.sum(AuditLog.details["bytes"].as_integer()).filter(ai).label("ai_bytes"))
    activity_by_user = activity.group_by(AuditLog.actor_id).subquery()
    reviews = select(QCRun.created_by.label("uid"), func.count().label("reviews")).where(
        QCRun.status == "done").group_by(QCRun.created_by).subquery()
    mvp_files = select(MvpUpload.user_id.label("uid"), func.count().label("files"), func.sum(MvpUpload.size).label("bytes"))
    mvp_files = mvp_files.where(MvpUpload.status == "ok").group_by(MvpUpload.user_id).subquery()
    documents = select(Document.uploaded_by.label("uid"), func.count().label("files"), func.sum(Document.size).label("bytes"))
    documents = documents.group_by(Document.uploaded_by).subquery()
    users = select(User.username, User.full_name, User.role, User.is_active, activity_by_user.c.last_seen,
                   func.coalesce(activity_by_user.c.logins, 0).label("logins"),
                   func.coalesce(activity_by_user.c.ai_calls, 0).label("ai_calls"),
                   func.coalesce(activity_by_user.c.ai_bytes, 0).label("ai_bytes"),
                   func.coalesce(reviews.c.reviews, 0).label("reviews"),
                   (func.coalesce(mvp_files.c.files, 0) + func.coalesce(documents.c.files, 0)).label("files"),
                   (func.coalesce(mvp_files.c.bytes, 0) + func.coalesce(documents.c.bytes, 0)).label("file_bytes"))
    users = (users.outerjoin(activity_by_user, activity_by_user.c.uid == User.id)
             .outerjoin(reviews, reviews.c.uid == User.id).outerjoin(mvp_files, mvp_files.c.uid == User.id)
             .outerjoin(documents, documents.c.uid == User.id))
    if query:
        users = users.where(User.username.icontains(query, autoescape=True) | User.full_name.icontains(query, autoescape=True))
    rows = db.execute(users.order_by(User.username).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE + 1)).all()
    verdicts = {verdict: count for verdict, count in db.execute(
        select(QCRun.verdict, func.count()).where(QCRun.status == "done").group_by(QCRun.verdict))}
    total_users, active_users = db.execute(select(func.count(User.id), func.count(User.id).filter(User.is_active))).one()
    ai_calls, ai_bytes = db.execute(select(func.count(), func.coalesce(func.sum(AuditLog.details["bytes"].as_integer()), 0))
                                   .where(ai)).one()
    pending = db.scalar(select(func.count(QCRuleFeedback.id)).where(QCRuleFeedback.status == "pending")) or 0
    return {"accounts": rows[:PAGE_SIZE], "has_next": len(rows) > PAGE_SIZE, "verdicts": verdicts, "pending": pending,
            "total_users": total_users, "active_users": active_users, "total_reviews": sum(verdicts.values()),
            "ai_calls": ai_calls, "ai_bytes": ai_bytes}
