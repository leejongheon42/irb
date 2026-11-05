"""데이터 거버넌스 화면: 반출 심사, 계약, 파기, 처리기록 대장."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..database import get_db
from ..governance import airlock, records
from ..models import Agreement, DestructionRecord, OutputRequest, Protocol, Role, User
from ..timeutil import parse_date, today
from ..workflow import service as wf
from .deps import client_ip, current_user, flash, render, verify_csrf

router = APIRouter()


def _is_data_admin(user: User) -> bool:
    return user.role in (Role.DATA_STEWARD, Role.ADMIN) or user.is_irb_staff


# ===================================================================== 반출 심사
@router.get("/outputs")
def outputs(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = db.scalars(select(OutputRequest).order_by(OutputRequest.id.desc())).all()
    if not _is_data_admin(user):
        items = [o for o in items if user.id in o.protocol.member_ids()]
    protocols = [p for p in db.scalars(select(Protocol).where(Protocol.approved_at.is_not(None)))
                 if p.is_active_approval and user.id in p.member_ids()]
    return render(request, "governance/outputs.html", db, user, items=items, protocols=protocols,
                  labels=airlock.STATUS_LABELS, policy=airlock._policy())


@router.post("/outputs", dependencies=[Depends(verify_csrf)])
async def output_submit(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()
    pid = form.get("protocol_id")
    p = db.get(Protocol, int(pid)) if pid and str(pid).isdigit() else None
    upload = form.get("file")
    if p is None or not hasattr(upload, "read"):
        flash(request, "과제와 결과 파일(CSV)을 선택하십시오.", "error")
        return RedirectResponse("/outputs", status_code=303)
    raw = await upload.read()
    if len(raw) > get_settings().max_upload_mb * 1024 * 1024:
        flash(request, "파일 크기 제한을 초과했습니다.", "error")
        return RedirectResponse("/outputs", status_code=303)
    try:
        req = airlock.submit_output(db, user, p, upload.filename, raw, str(form.get("description", "")), client_ip(request))
    except wf.WorkflowError as e:
        db.rollback()
        flash(request, str(e), "error")
        return RedirectResponse("/outputs", status_code=303)
    db.commit()
    flash(request, f"반출 심사를 요청했습니다(자동점검: {req.check['verdict']}).", "success")
    return RedirectResponse(f"/outputs/{req.id}", status_code=303)


def _output(db: Session, user: User, oid: int) -> OutputRequest:
    req = db.get(OutputRequest, oid)
    if req is None:
        raise HTTPException(404)
    if not (_is_data_admin(user) or user.id in req.protocol.member_ids()):
        raise HTTPException(403, "열람 권한이 없습니다.")
    return req


@router.get("/outputs/{oid}")
def output_detail(request: Request, oid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    req = _output(db, user, oid)
    return render(request, "governance/output_detail.html", db, user, req=req, labels=airlock.STATUS_LABELS,
                  downloadable=airlock.downloadable_path(req))


@router.post("/outputs/{oid}/review", dependencies=[Depends(verify_csrf)])
async def output_review(request: Request, oid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    req = _output(db, user, oid)
    form = await request.form()
    try:
        airlock.review_output(db, user, req, str(form.get("action", "")), str(form.get("comment", "")), client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/outputs/{oid}", status_code=303)
    db.commit()
    flash(request, f"반출 심사 결과: {airlock.STATUS_LABELS[req.status]}", "success")
    return RedirectResponse(f"/outputs/{oid}", status_code=303)


@router.get("/outputs/{oid}/download")
def output_download(request: Request, oid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    req = _output(db, user, oid)
    path = airlock.downloadable_path(req)
    if not path:
        raise HTTPException(403, "승인된 반출 결과만 다운로드할 수 있습니다.")
    audit.record(db, user, "output_download", "output", req.id, {"status": req.status}, client_ip(request))
    db.commit()
    suffix = "_suppressed" if req.status == "approved_suppressed" else ""
    return FileResponse(path, filename=f"{Path(req.filename).stem}{suffix}.csv", media_type="text/csv")


# ===================================================================== 계약(DUA)
@router.get("/governance/agreements")
def agreements(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not _is_data_admin(user):
        raise HTTPException(403, "계약 관리 권한이 없습니다.")
    items = db.scalars(select(Agreement).order_by(Agreement.end_date)).all()
    protocols = db.scalars(select(Protocol).where(Protocol.protocol_no.is_not(None)).order_by(Protocol.id.desc())).all()
    return render(request, "governance/agreements.html", db, user, items=items, protocols=protocols)


@router.post("/governance/agreements", dependencies=[Depends(verify_csrf)])
async def agreement_create(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()
    pid = form.get("protocol_id")
    upload = form.get("file")
    file = None
    if hasattr(upload, "read") and getattr(upload, "filename", ""):
        file = (upload.filename, await upload.read())
    data = {
        "protocol_id": int(pid) if pid and str(pid).isdigit() else None,
        "agreement_type": str(form.get("agreement_type", "data_provision")),
        "counterparty": str(form.get("counterparty", "")).strip(),
        "title": str(form.get("title", "")).strip(),
        "permitted_use": str(form.get("permitted_use", "")).strip(),
        "terms": [str(t) for t in form.getlist("terms")],
        "start_date": parse_date(str(form.get("start_date", ""))),
        "end_date": parse_date(str(form.get("end_date", ""))),
    }
    try:
        records.create_agreement(db, user, data, file, client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/governance/agreements", status_code=303)
    db.commit()
    flash(request, "계약을 등록했습니다. 만료 60·30일 전 자동 알림이 발송됩니다.", "success")
    return RedirectResponse("/governance/agreements", status_code=303)


@router.post("/governance/agreements/{aid}/terminate", dependencies=[Depends(verify_csrf)])
def agreement_terminate(request: Request, aid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = db.get(Agreement, aid)
    if a is None:
        raise HTTPException(404)
    try:
        records.terminate_agreement(db, user, a, client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/governance/agreements", status_code=303)
    db.commit()
    return RedirectResponse("/governance/agreements", status_code=303)


@router.get("/governance/agreements/{aid}/file")
def agreement_file(aid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = db.get(Agreement, aid)
    if a is None or not a.document_path:
        raise HTTPException(404)
    if not (_is_data_admin(user) or (a.protocol and user.id in a.protocol.member_ids())):
        raise HTTPException(403)
    return FileResponse(a.document_path, filename=a.document_name or "agreement")


# ===================================================================== 파기 관리
@router.get("/governance/destruction")
def destruction(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = db.scalars(select(DestructionRecord).order_by(DestructionRecord.due_date)).all()
    if not _is_data_admin(user):
        items = [r for r in items if user.id in r.protocol.member_ids()]
    return render(request, "governance/destruction.html", db, user, items=items)


@router.post("/governance/destruction/{rid}", dependencies=[Depends(verify_csrf)])
async def destruction_certify(request: Request, rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rec = db.get(DestructionRecord, rid)
    if rec is None:
        raise HTTPException(404)
    form = await request.form()
    upload = form.get("file")
    file = (upload.filename, await upload.read()) if hasattr(upload, "read") and getattr(upload, "filename", "") else None
    try:
        records.certify_destruction(db, user, rec, parse_date(str(form.get("destroyed_at", ""))) or today(),
                                    str(form.get("method", "")), file, client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/governance/destruction", status_code=303)
    db.commit()
    flash(request, "파기 확인이 등록되었습니다.", "success")
    return RedirectResponse("/governance/destruction", status_code=303)


@router.get("/governance/destruction/{rid}/certificate")
def destruction_certificate(rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rec = db.get(DestructionRecord, rid)
    if rec is None or not rec.certificate_path:
        raise HTTPException(404)
    if not (_is_data_admin(user) or user.id in rec.protocol.member_ids()):
        raise HTTPException(403)
    return FileResponse(rec.certificate_path, filename=rec.certificate_name or "certificate")


# ===================================================================== 처리기록 대장
LEDGER_COLUMNS = [
    ("id", "작업번호"), ("processed_at", "처리일시"), ("protocol_no", "과제번호"), ("purpose", "처리 목적"),
    ("source", "원본"), ("items", "처리 항목(제공 항목)"), ("methods", "가명처리 방법"), ("rows", "건수"),
    ("verdict", "자동판정"), ("drb", "적정성 검토"), ("recipient", "제공받는 자"), ("released_at", "제공일시"),
    ("retention", "보관기간"), ("processor", "처리자"),
]


@router.get("/governance/ledger")
def ledger(request: Request, year: int | None = None, fmt: str = "", user: User = Depends(current_user),
           db: Session = Depends(get_db)):
    if not _is_data_admin(user):
        raise HTTPException(403, "처리기록 대장 열람 권한이 없습니다.")
    rows = records.ledger_rows(db, year)
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([label for _, label in LEDGER_COLUMNS])
        for r in rows:
            w.writerow([r[k] for k, _ in LEDGER_COLUMNS])
        audit.record(db, user, "export", "ledger", None, {"year": year, "rows": len(rows)}, client_ip(request))
        db.commit()
        return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": "attachment; filename=pseudonym_ledger.csv"})
    return render(request, "governance/ledger.html", db, user, rows=rows, columns=LEDGER_COLUMNS, year=year)
