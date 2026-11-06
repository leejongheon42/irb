"""자료 제공 계약(DUA), 자료 파기, 가명정보 처리기록 대장, DRB 적정성 검토."""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..engine.ruleset import get_ruleset
from ..models import (
    Agreement,
    DeidJob,
    DestructionRecord,
    Protocol,
    ProtocolStatus,
    Role,
    User,
)
from ..notifications import notify, staff
from ..timeutil import now, today
from ..workflow import service as wf

ALLOWED_ATTACHMENTS = {".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".jpg", ".jpeg", ".png", ".zip", ".tif", ".tiff"}


def _store(folder: str, filename: str, raw: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_ATTACHMENTS:
        raise wf.WorkflowError(f"허용되지 않는 첨부 형식입니다({suffix or '확장자 없음'}). PDF·HWP·DOCX·이미지·ZIP만 가능합니다.")
    if len(raw) > get_settings().max_upload_mb * 1024 * 1024:
        raise wf.WorkflowError("파일 크기 제한을 초과했습니다.")
    base = get_settings().data_dir / folder
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{uuid.uuid4().hex}_{Path(filename).suffix or '.bin'}"
    path.write_bytes(raw)
    return str(path)


# ================================================================ 계약(DUA)
def _require_contract_admin(user: User) -> None:
    if not (user.is_irb_staff or user.role == Role.DATA_STEWARD):
        raise wf.PermissionDenied("계약 관리 권한이 없습니다(IRB 사무국·데이터관리자).")


def create_agreement(db: Session, user: User, data: dict[str, Any], file: tuple[str, bytes] | None = None,
                     ip: str = "") -> Agreement:
    _require_contract_admin(user)
    if not data.get("counterparty"):
        raise wf.WorkflowError("계약 상대 기관을 입력하십시오.")
    if data.get("start_date") and data.get("end_date") and data["end_date"] < data["start_date"]:
        raise wf.WorkflowError("계약 종료일이 시작일보다 앞섭니다.")
    a = Agreement(created_by=user.id, **data)
    if file and file[1]:
        a.document_path = _store("agreements", file[0], file[1])
        a.document_name = Path(file[0]).name
    db.add(a)
    db.flush()
    audit.record(db, user, "agreement_create", "agreement", a.id,
                 {"protocol_id": a.protocol_id, "type": a.agreement_type, "end": str(a.end_date)}, ip)
    return a


def terminate_agreement(db: Session, user: User, a: Agreement, ip: str = "") -> None:
    _require_contract_admin(user)
    a.status = "terminated"
    audit.record(db, user, "agreement_terminate", "agreement", a.id, {}, ip)


def effective_agreements(db: Session, protocol: Protocol) -> list[Agreement]:
    return [a for a in db.scalars(select(Agreement).where(Agreement.protocol_id == protocol.id)) if a.is_effective]


def transfer_gate(db: Session, protocol: Protocol) -> str | None:
    """외부 제공 과제는 유효한 자료 제공 계약이 있어야 제공 가능. 문제 시 사유 반환."""
    if not protocol.external_transfer:
        return None
    types = {a.agreement_type for a in effective_agreements(db, protocol)}
    if not types & {"data_provision", "collaboration", "consignment"}:
        return "외부 제공 과제이나 유효한 자료 제공(공동연구·위탁) 계약이 없습니다."
    return None


# ================================================================ 파기
def ensure_destruction_record(db: Session, protocol: Protocol, report: dict[str, Any] | None = None) -> DestructionRecord:
    rec = db.scalars(select(DestructionRecord).where(DestructionRecord.protocol_id == protocol.id)).first()
    if rec:
        return rec
    years = max(0, int(protocol.retention_years or 0))
    base = protocol.study_end if protocol.study_end and protocol.study_end < today() else today()
    rec = DestructionRecord(protocol_id=protocol.id, due_date=base + timedelta(days=365 * years))
    if report and report.get("data_destroyed"):
        rec.destroyed_at = date.fromisoformat(report["destruction_date"]) if report.get("destruction_date") else today()
        rec.method = "종료보고 시 파기 완료 보고"
    db.add(rec)
    db.flush()
    return rec


def certify_destruction(db: Session, user: User, rec: DestructionRecord, destroyed_at: date, method: str,
                        file: tuple[str, bytes] | None = None, ip: str = "") -> None:
    p = rec.protocol
    if not (user.id in p.member_ids() or user.is_irb_staff or user.role == Role.DATA_STEWARD):
        raise wf.PermissionDenied("파기 확인 권한이 없습니다.")
    if not method.strip():
        raise wf.WorkflowError("파기 방법을 입력하십시오.")
    rec.destroyed_at = destroyed_at
    rec.method = method.strip()
    rec.certified_by = user.id
    if file and file[1]:
        rec.certificate_path = _store("destruction", file[0], file[1])
        rec.certificate_name = Path(file[0]).name
    audit.record(db, user, "destruction_certify", "protocol", p.id,
                 {"destroyed_at": destroyed_at.isoformat(), "method": rec.method,
                  "certificate_sha256": hashlib.sha256(file[1]).hexdigest() if file and file[1] else None}, ip)
    notify(db, staff(db), f"{p.protocol_no} 자료 파기 확인 등록", "/governance/destruction")


# ================================================================ DRB 적정성 검토
def drb_review(db: Session, user: User, job: DeidJob, approve: bool, comment: str = "", ip: str = "") -> None:
    """가명처리 적정성 검토(데이터심의위원회). 처리자(데이터관리자) 본인은 검토할 수 없다."""
    if user.role not in (Role.REVIEWER, Role.CHAIR, Role.COORDINATOR, Role.ADMIN):
        raise wf.PermissionDenied("적정성 검토는 위원(DRB)만 할 수 있습니다.")
    if user.id == job.created_by:
        raise wf.PermissionDenied("처리자 본인은 적정성 검토를 할 수 없습니다(처리자·검토자 분리).")
    if job.status not in ("processed", "released") or not job.report:
        raise wf.WorkflowError("가명처리가 완료된 작업만 검토할 수 있습니다.")
    if approve and job.report.get("verdict") == "부적정":
        raise wf.WorkflowError("자동 판정이 '부적정'인 결과는 승인할 수 없습니다.")
    if not approve and not comment.strip():
        raise wf.WorkflowError("부적정 사유를 입력하십시오.")
    job.drb_status = "approved" if approve else "rejected"
    job.drb_reviewer_id = user.id
    job.drb_reviewed_at = now()
    job.drb_comment = comment.strip()
    audit.record(db, user, "drb_review", "deid_job", job.id,
                 {"approved": approve, "output_sha256": job.report.get("output_sha256")}, ip)
    notify(db, [job.created_by], f"가명처리 #{job.id} 적정성 검토: {'적정' if approve else '부적정'}", f"/deid/{job.id}")


def reset_drb(job: DeidJob) -> None:
    job.drb_status = "pending"
    job.drb_reviewer_id = None
    job.drb_reviewed_at = None
    job.drb_comment = ""


# ================================================================ 처리기록 대장
def ledger_rows(db: Session, year: int | None = None) -> list[dict[str, Any]]:
    """가명정보 처리 기록(개인정보 보호법 제28조의4제2항): 처리 목적, 처리 항목, 제공받는 자, 처리일시 등."""
    rs = get_ruleset()
    rows = []
    for job in db.scalars(select(DeidJob).order_by(DeidJob.id)):
        if year and job.created_at.year != year:
            continue
        rep = job.report or {}
        p = job.protocol
        kept = [a["column"] for a in rep.get("column_actions", []) if a["action"] != "delete"]
        rows.append({
            "id": job.id,
            "processed_at": (rep.get("processed_at") or job.created_at.isoformat())[:16].replace("T", " "),
            "protocol_no": p.protocol_no if p else "",
            "purpose": job.purpose or (f"과학적 연구: {p.title}" if p else "시험 처리"),
            "source": job.original_filename,
            "items": ", ".join(kept),
            "methods": "; ".join(f"{a['column']}={a['description']}" for a in rep.get("column_actions", [])
                                 if a["action"] != "keep"),
            "rows": rep.get("output_rows", job.row_count),
            "verdict": rep.get("verdict", "미처리"),
            "drb": {"approved": "적정", "rejected": "부적정"}.get(job.drb_status, "검토 대기"),
            "recipient": (f"{p.pi.full_name}({p.department})" if p and job.released_to else ""),
            "released_at": job.released_at.strftime("%Y-%m-%d %H:%M") if job.released_at else "",
            "retention": f"{p.retention_years}년" if p else "",
            "processor": job.creator.full_name,
            "deid_level": rs.label("deid_levels", p.deid_level) if p else "",
        })
    return rows


# ================================================================ 스케줄러 보조
def governance_reminders(db: Session) -> dict[str, int]:
    rs = get_ruleset()
    t = today()
    stats = {"agreement_notices": 0, "destruction_notices": 0}
    days = sorted(rs.data.get("agreement_policy", {}).get("reminder_days", [60, 30]))
    for a in db.scalars(select(Agreement).where(Agreement.status == "active", Agreement.end_date.is_not(None))):
        left = (a.end_date - t).days
        for d in days:
            if 0 <= left <= d:
                targets = list(staff(db))
                if a.protocol:
                    targets += [u for u in (db.get(User, uid) for uid in a.protocol.member_ids()) if u]
                stats["agreement_notices"] += notify(
                    db, targets, f"계약 만료 {left}일 전: {a.counterparty} ({a.end_date})", "/governance/agreements",
                    dedup_key=f"agreement:{a.id}:{a.end_date}:{d}")
                break
    ddays = sorted(rs.data.get("destruction_policy", {}).get("reminder_days", [60, 30, 7]))
    for rec in db.scalars(select(DestructionRecord).where(DestructionRecord.destroyed_at.is_(None))):
        left = (rec.due_date - t).days
        p = rec.protocol
        if left < 0:
            stats["destruction_notices"] += notify(
                db, list(p.member_ids()) + staff(db), f"{p.protocol_no} 자료 파기 기한 경과({rec.due_date}) - 파기 후 확인서를 등록하십시오.",
                "/governance/destruction", dedup_key=f"destroy-overdue:{rec.id}:{t.isoformat()[:7]}")
            continue
        for d in ddays:
            if left <= d:
                stats["destruction_notices"] += notify(
                    db, p.member_ids(), f"{p.protocol_no} 자료 파기 기한 {left}일 전({rec.due_date})",
                    "/governance/destruction", dedup_key=f"destroy:{rec.id}:{rec.due_date}:{d}")
                break
    return stats


def closed_protocol_hook(db: Session, protocol: Protocol, report: dict[str, Any] | None) -> None:
    if protocol.status == ProtocolStatus.CLOSED and protocol.deid_level != "anonymized":
        ensure_destruction_record(db, protocol, report)
