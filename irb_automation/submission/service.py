"""연구팀 서류함: 업로드 검증·저장·버전 등록, 연구팀 의견, 서류 현황판(필요 서류 체크리스트)."""
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
from ..models import Document, Protocol, ProtocolStatus, User
from ..notifications import notify
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
def can_participate(db: Session, user: User, protocol: Protocol) -> bool:
    """서류 열람·의견 권한: 연구팀과 관리자."""
    return wf.is_member(user, protocol) or user.is_admin


def can_upload(db: Session, user: User, protocol: Protocol, doc_type: str) -> tuple[bool, str]:
    """IRB에 이미 승인된 서류가 몰래 바뀌지 않도록, 승인 이후에는 변경 준비 상태에서만 교체."""
    if not wf.is_member(user, protocol):
        return False, "연구책임자·공동연구자만 서류를 올릴 수 있습니다."
    if protocol.is_editable or protocol.status == S.SUBMITTED:
        return True, ""
    if doc_type in policy()["report_doc_types"]:
        return True, ""
    return False, (f"'{protocol.status.label}' 상태에서는 '{doc_label(doc_type)}'을(를) 교체할 수 없습니다. "
                   "변경 준비를 시작한 뒤 올리십시오.")


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
    audit.record(db, user, "document_upload", "protocol", protocol.id,
                 {"doc_id": doc.id, "doc_type": doc_type, "sha256": doc.sha256, "version": doc.version,
                  "source": source, "warnings": len(result.warnings)}, ip)
    if run_hook:
        after_document_saved(db, user, doc)
    return doc, result


# ------------------------------------------------------------------ 연구팀 의견
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


# ------------------------------------------------------------------ 현황판
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
    counts: Counter = Counter()
    counts["missing"] = sum(1 for r in rows if r["required"] and r["current"] is None)
    qc: Counter = Counter()
    for r in rows:
        if r["current"] is None:
            continue
        q = (r["current"].validation or {}).get("qc")
        qc["unchecked" if not q else ("issues" if q.get("critical") or q.get("major") else "clean")] += 1
    upload_types = [(k, label) for k, label in get_ruleset().options("document_types")
                    if can_upload(db, user, protocol, k)[0]]
    return {
        "rows": rows,
        "counts": counts,
        "qc": qc,
        "can_participate": can_participate(db, user, protocol),
        "upload_types": upload_types,
        "max_files": int(policy()["max_files_per_upload"]),
    }
