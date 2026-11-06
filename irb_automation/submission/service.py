"""웹 문서 제출 서비스: 제출 권한, 검증·저장·버전 등록, 문서별 검토, 의견, 검토 대기열, 제출 현황판."""
from __future__ import annotations

import logging
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..engine.ruleset import get_ruleset
from ..models import Document, Protocol, ProtocolStatus, Role, User
from ..notifications import notify, staff
from ..timeutil import now
from ..workflow import service as wf
from .hooks import after_document_saved
from .models import DocumentComment
from .validate import ValidationResult, validate_upload
from .versions import latest_by_type, register_new_version, versions_of

log = logging.getLogger(__name__)
S = ProtocolStatus

DEFAULT_POLICY: dict[str, Any] = {
    "max_files_per_upload": 20,
    "reject_encrypted_pdf": True,
    "warn_pdf_active_content": True,
    "stamp_doc_types": ["consent_form", "protocol"],
    "stamp_all_pages": True,
    "report_doc_types": ["progress_report", "final_report", "destruction_certificate", "other"],
}


class SubmissionError(Exception):
    def __init__(self, message: str, result: ValidationResult | None = None):
        super().__init__(message)
        self.result = result


def policy() -> dict[str, Any]:
    return {**DEFAULT_POLICY, **get_ruleset().policy("submission")}


def doc_label(doc_type: str) -> str:
    return get_ruleset().label("document_types", doc_type)


# ------------------------------------------------------------------ 권한
def can_review(db: Session, user: User, protocol: Protocol) -> bool:
    if user.is_irb_staff:
        return True
    return user.role in (Role.REVIEWER, Role.CHAIR) and wf.is_assigned_reviewer(db, user, protocol)


def can_participate(db: Session, user: User, protocol: Protocol) -> bool:
    """문서 의견·합본 등 검토 참여 권한: 연구팀, 사무국, 배정 위원."""
    return wf.is_member(user, protocol) or can_review(db, user, protocol)


def can_upload(db: Session, user: User, protocol: Protocol, doc_type: str) -> tuple[bool, str]:
    """심의 중 무단 교체를 막기 위한 제출 가능 여부."""
    if user.is_irb_staff:
        return True, ""
    if not wf.is_member(user, protocol):
        return False, "연구책임자·공동연구자만 서류를 제출할 수 있습니다."
    label = doc_label(doc_type)
    current = latest_by_type(list(protocol.documents)).get(doc_type)
    if current is not None and current.status == "revision_requested":
        return True, ""
    if protocol.is_editable:
        return True, ""
    if protocol.status in (S.SUBMITTED, S.UNDER_REVIEW):
        if current is None:
            return True, ""
        return False, f"심의 중에는 보완 요청된 서류만 교체할 수 있습니다('{label}')."
    if doc_type in policy()["report_doc_types"]:
        return True, ""
    return False, (f"'{protocol.status.label}' 상태에서는 '{label}'을(를) 새로 제출할 수 없습니다. "
                   "변경 신청 후 제출하십시오.")


# ------------------------------------------------------------------ 저장
def store_document(
    db: Session,
    user: User,
    protocol: Protocol,
    *,
    doc_type: str,
    filename: str,
    raw: bytes,
    source: str = "upload",
    ip: str = "",
    run_hook: bool = True,
    check_permission: bool = True,
    status: str | None = None,
) -> tuple[Document, ValidationResult]:
    """검증 → 저장 → 현행본 등록 → 감사 기록 → 후속 처리(파이프라인)."""
    if doc_type not in get_ruleset().catalog("document_types"):
        doc_type = "other"
    if check_permission:
        ok, reason = can_upload(db, user, protocol, doc_type)
        if not ok:
            raise SubmissionError(reason)
    settings = get_settings()
    existing = [(d.sha256, f"{doc_label(d.doc_type)} v{d.version}") for d in protocol.documents]
    result = validate_upload(filename, raw, max_bytes=settings.max_upload_mb * 1024 * 1024,
                             existing=existing, policy=policy())
    if not result.ok:
        audit.record(db, user, "document_reject", "protocol", protocol.id,
                     {"filename": result.filename, "kind": result.kind, "sha256": result.sha256,
                      "errors": result.errors}, ip)
        raise SubmissionError(" ".join(result.errors), result)

    previous = latest_by_type(list(protocol.documents)).get(doc_type)
    previous_status = previous.status if previous else None
    folder = settings.upload_dir / str(protocol.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}_{result.safe_name}"
    path.write_bytes(raw)
    doc = Document(
        protocol=protocol, doc_type=doc_type, filename=result.filename, stored_path=str(path),
        sha256=result.sha256, size=result.size, version=1, uploaded_by=user.id, source=source,
        mime_type=result.mime_type, page_count=result.page_count, validation=result.to_dict(),
        status=status or "submitted",
    )
    db.add(doc)
    db.flush()
    register_new_version(db, doc)
    if previous is not None and previous_status == "revision_requested":
        db.add(DocumentComment(document_id=doc.id, protocol_id=protocol.id, author_id=user.id, kind="new_version",
                               body=f"보완 요청(v{previous.version})에 대한 새 버전을 제출했습니다."))
        targets = {u.id for u in staff(db)}
        if previous.reviewed_by:
            targets.add(previous.reviewed_by)
        targets.discard(user.id)
        notify(db, targets, f"{protocol.protocol_no or protocol.title} 보완 서류 제출: {doc_label(doc_type)} v{doc.version}",
               f"/documents/{doc.id}/preview")
    audit.record(db, user, "document_upload", "protocol", protocol.id,
                 {"doc_id": doc.id, "doc_type": doc_type, "sha256": doc.sha256, "version": doc.version,
                  "source": source, "warnings": len(result.warnings)}, ip)
    if run_hook:
        after_document_saved(db, user, doc)
    return doc, result


# ------------------------------------------------------------------ 문서별 검토
def review_document(db: Session, user: User, doc: Document, action: str, note: str = "", ip: str = "") -> None:
    protocol = doc.protocol
    if not can_review(db, user, protocol):
        raise wf.PermissionDenied("서류 검토 권한이 없습니다(IRB 사무국·배정 심의위원).")
    latest = latest_by_type(list(protocol.documents)).get(doc.doc_type)
    if doc.status == "superseded" or latest is None or latest.id != doc.id:
        raise wf.WorkflowError("이전 버전은 검토할 수 없습니다. 최신 버전을 검토하십시오.")
    note = note.strip()
    if action == "accept":
        doc.status = "accepted"
    elif action == "request_revision":
        if not note:
            raise wf.WorkflowError("보완 요청 사유를 입력하십시오.")
        doc.status = "revision_requested"
    else:
        raise wf.WorkflowError("알 수 없는 처리입니다.")
    doc.review_note = note
    doc.reviewed_by = user.id
    doc.reviewed_at = now()
    db.add(DocumentComment(document_id=doc.id, protocol_id=protocol.id, author_id=user.id,
                           kind=action, body=note or "확인했습니다."))
    audit.record(db, user, "document_review", "protocol", protocol.id,
                 {"doc_id": doc.id, "doc_type": doc.doc_type, "version": doc.version, "action": action,
                  "note": note[:200]}, ip)
    if action == "request_revision":
        notify(db, protocol.member_ids(),
               f"{protocol.protocol_no or protocol.title} 서류 보완 요청: {doc_label(doc.doc_type)} - {note[:60]}",
               f"/documents/{doc.id}/preview")


def add_comment(db: Session, user: User, doc: Document, body: str, ip: str = "") -> DocumentComment:
    protocol = doc.protocol
    if not can_participate(db, user, protocol):
        raise wf.PermissionDenied("이 서류에 의견을 남길 권한이 없습니다.")
    body = body.strip()
    if not body:
        raise wf.WorkflowError("내용을 입력하십시오.")
    c = DocumentComment(document_id=doc.id, protocol_id=protocol.id, author_id=user.id, kind="comment",
                        body=body[:4000])
    db.add(c)
    db.flush()
    audit.record(db, user, "document_comment", "protocol", protocol.id, {"doc_id": doc.id, "comment_id": c.id}, ip)
    if wf.is_member(user, protocol):
        targets = {u.id for u in staff(db)}
        sub = protocol.current_submission
        if sub:
            targets |= {a.reviewer_id for a in sub.assignments}
        if doc.reviewed_by:
            targets.add(doc.reviewed_by)
    else:
        targets = set(protocol.member_ids())
    targets.discard(user.id)
    notify(db, targets, f"{protocol.protocol_no or protocol.title} 서류 의견({doc_label(doc.doc_type)}): {body[:50]}",
           f"/documents/{doc.id}/preview")
    return c


def comments_for(db: Session, protocol: Protocol, doc_type: str) -> list[DocumentComment]:
    """같은 서류 종류의 모든 버전에 달린 의견(버전을 넘는 대화 스레드)."""
    ids = [d.id for d in protocol.documents if d.doc_type == doc_type]
    if not ids:
        return []
    return list(db.scalars(
        select(DocumentComment).where(DocumentComment.document_id.in_(ids)).order_by(DocumentComment.id)
    ))


# ------------------------------------------------------------------ 대기열·현황판
def review_queue(db: Session, user: User) -> dict[str, list[Document]]:
    docs = db.scalars(
        select(Document).join(Protocol)
        .where(Document.status.in_(["submitted", "revision_requested"]), Protocol.status != S.DRAFT)
        .order_by(Document.uploaded_at)
    ).all()
    latest_cache: dict[int, dict[str, Document]] = {}
    pending, waiting = [], []
    for d in docs:
        p = d.protocol
        if p.id not in latest_cache:
            latest_cache[p.id] = latest_by_type(list(p.documents))
        if latest_cache[p.id].get(d.doc_type) is not d:
            continue
        if not user.is_irb_staff and not wf.is_assigned_reviewer(db, user, p):
            continue
        (pending if d.status == "submitted" else waiting).append(d)
    return {"pending": pending, "waiting": waiting}


def build_board(db: Session, protocol: Protocol, user: User) -> dict[str, Any]:
    """과제 서류 탭의 체크리스트형 제출 현황판."""
    docs = list(protocol.documents)
    latest = latest_by_type(docs)
    checklist: list[dict[str, Any]] = []
    try:
        if protocol.is_editable:
            checklist = wf.preview(db, protocol)["checklist"]
        elif protocol.current_submission and protocol.current_submission.latest_prereview:
            checklist = protocol.current_submission.latest_prereview.result.get("checklist", [])
    except Exception:  # 현황판은 사전검토 실패와 무관하게 표시
        log.exception("checklist for board failed")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    def row(doc_type: str, label: str, required: bool) -> dict[str, Any]:
        current = latest.get(doc_type)
        return {
            "doc_type": doc_type,
            "label": label,
            "required": required,
            "current": current,
            "versions": versions_of(docs, doc_type),
            "can_upload": can_upload(db, user, protocol, doc_type)[0],
        }

    for c in checklist:
        if c["doc"] in seen:
            continue
        seen.add(c["doc"])
        rows.append(row(c["doc"], c["label"], True))
    for doc_type in latest:
        if doc_type not in seen:
            rows.append(row(doc_type, doc_label(doc_type), False))
    counts = Counter(r["current"].status for r in rows if r["current"] is not None)
    counts["missing"] = sum(1 for r in rows if r["required"] and r["current"] is None)
    upload_types = [(k, label) for k, label in get_ruleset().options("document_types")
                    if can_upload(db, user, protocol, k)[0]]
    return {
        "rows": rows,
        "counts": counts,
        "can_review": can_review(db, user, protocol),
        "can_participate": can_participate(db, user, protocol),
        "upload_types": upload_types,
        "max_files": int(policy()["max_files_per_upload"]),
    }
