"""가명처리 도구 및 IRB 승인 연동 데이터 제공."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..database import get_db
from ..deid import pipeline
from ..deid.detector import analyze_table
from ..deid.transforms import ACTIONS
from ..engine.ruleset import get_ruleset
from ..governance import records
from ..models import DeidJob, Protocol, Role, User
from ..notifications import notify
from ..timeutil import now
from ..workflow import service as wf
from .deps import client_ip, current_user, flash, render, require_roles, verify_csrf

router = APIRouter()
steward = require_roles(Role.DATA_STEWARD)
DRB_ROLES = (Role.REVIEWER, Role.CHAIR, Role.COORDINATOR)
drb_member = require_roles(*DRB_ROLES)


def _job(db: Session, jid: int) -> DeidJob:
    job = db.get(DeidJob, jid)
    if job is None:
        raise HTTPException(404)
    return job


def _salt(job: DeidJob) -> str:
    # 과제별로 다른 가명이 생성되도록 과제 ID를 솔트로 사용(과제 간 결합 방지)
    return f"protocol:{job.protocol_id}" if job.protocol_id else f"job:{job.id}"


@router.get("/deid")
def deid_list(request: Request, user: User = Depends(steward), db: Session = Depends(get_db)):
    jobs = db.scalars(select(DeidJob).order_by(DeidJob.id.desc())).all()
    return render(request, "deid/list.html", db, user, jobs=jobs)


@router.get("/deid/new")
def deid_new(request: Request, protocol_id: int | None = None, user: User = Depends(steward), db: Session = Depends(get_db)):
    protocols = [p for p in db.scalars(select(Protocol).where(Protocol.approved_at.is_not(None))) if p.is_active_approval]
    return render(request, "deid/new.html", db, user, protocols=protocols, selected=protocol_id)


@router.post("/deid/new", dependencies=[Depends(verify_csrf)])
async def deid_upload(request: Request, user: User = Depends(steward), db: Session = Depends(get_db)):
    form = await request.form()
    upload = form.get("file")
    if not hasattr(upload, "read") or not getattr(upload, "filename", ""):
        flash(request, "CSV 파일을 선택하십시오.", "error")
        return RedirectResponse("/deid/new", status_code=303)
    raw = await upload.read()
    settings = get_settings()
    if len(raw) > settings.max_upload_mb * 1024 * 1024:
        flash(request, f"파일 크기 제한({settings.max_upload_mb}MB)을 초과했습니다.", "error")
        return RedirectResponse("/deid/new", status_code=303)
    try:
        headers, rows, enc = pipeline.read_csv_bytes(raw)
    except ValueError as e:
        flash(request, f"파일을 읽을 수 없습니다: {e}", "error")
        return RedirectResponse("/deid/new", status_code=303)
    pid = form.get("protocol_id")
    protocol = db.get(Protocol, int(pid)) if pid and str(pid).isdigit() else None
    analysis = analyze_table(headers, rows)
    job = DeidJob(
        protocol_id=protocol.id if protocol else None,
        created_by=user.id,
        original_filename=Path(upload.filename).name,
        input_path="",
        input_sha256=hashlib.sha256(raw).hexdigest(),
        encoding=enc,
        row_count=len(rows),
        analysis=analysis,
        purpose=str(form.get("purpose", "")).strip()[:300],
    )
    db.add(job)
    db.flush()
    folder = settings.deid_dir / str(job.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "input.csv"
    path.write_bytes(raw)
    job.input_path = str(path)
    job.config = pipeline.default_config(analysis, get_ruleset().deid_policy)
    audit.record(db, user, "deid_upload", "deid_job", job.id,
                 {"file": job.original_filename, "sha256": job.input_sha256, "rows": len(rows),
                  "protocol_id": job.protocol_id}, client_ip(request))
    db.commit()
    flash(request, f"{len(rows):,}행 분석 완료 - 직접식별자 {len(analysis['direct_identifiers'])}개 탐지.", "success")
    return RedirectResponse(f"/deid/{job.id}", status_code=303)


@router.get("/deid/{jid}")
def deid_detail(request: Request, jid: int, user: User = Depends(steward), db: Session = Depends(get_db)):
    job = _job(db, jid)
    return render(request, "deid/detail.html", db, user, job=job, actions=ACTIONS,
                  policy=get_ruleset().deid_policy)


@router.post("/deid/{jid}/process", dependencies=[Depends(verify_csrf)])
async def deid_process(request: Request, jid: int, user: User = Depends(steward), db: Session = Depends(get_db)):
    job = _job(db, jid)
    if not job.input_path or not Path(job.input_path).exists():
        flash(request, "원본 파일이 파기되어 재처리할 수 없습니다.", "error")
        return RedirectResponse(f"/deid/{jid}", status_code=303)
    form = await request.form()
    columns = {}
    for col in job.analysis["columns"]:
        name = col["name"]
        key = f"col_{col_index(job, name)}"
        action = str(form.get(f"{key}_action", "keep"))
        if action not in ACTIONS:
            action = "keep"
        params = {}
        for p in ("max_days", "width", "top_code", "level", "unit", "base", "lower", "upper", "prefix", "kind"):
            v = form.get(f"{key}_{p}")
            if v not in (None, ""):
                params[p] = v
        columns[name] = {"action": action, "params": params}
    headers_ = [c["name"] for c in job.analysis["columns"]]
    config = {
        "columns": columns,
        "subject_column": str(form.get("subject_column") or "") or None,
        "quasi_identifiers": [h for h in form.getlist("qi") if h in headers_],
        "sensitive_attributes": [h for h in form.getlist("sa") if h in headers_],
        "suppress_below_k": form.get("suppress_below_k") == "on",
        "k_threshold": int(form.get("k_threshold") or get_ruleset().deid_policy.get("k_threshold", 5)),
        "reference_date": str(form.get("reference_date") or now().date().isoformat()),
    }
    settings = get_settings()
    headers, rows, _ = pipeline.read_csv_path(Path(job.input_path))
    result = pipeline.process(
        headers, rows, config,
        secret=settings.pseudonym_secret, project_salt=_salt(job),
        policy=get_ruleset().deid_policy, analysis=job.analysis,
    )
    out = settings.deid_dir / str(job.id) / "output.csv"
    out.write_bytes(pipeline.write_csv(result.headers, result.rows))
    job.output_path = str(out)
    if result.mappings:
        mp = settings.mapping_dir / f"job{job.id}_mapping.json"
        mp.write_text(json.dumps(result.mappings, ensure_ascii=False), encoding="utf-8")
        mp.chmod(0o600)
        job.mapping_path = str(mp)
    job.config = config
    result.report["output_sha256"] = hashlib.sha256(out.read_bytes()).hexdigest()
    result.report["processed_at"] = now().isoformat()
    result.report["processed_by"] = user.full_name
    job.report = result.report
    job.status = "processed"
    job.released_to = None
    job.released_at = None
    records.reset_drb(job)  # 재처리 시 적정성 검토를 다시 받아야 함
    audit.record(db, user, "deid_process", "deid_job", job.id,
                 {"verdict": result.report["verdict"], "output_rows": result.report["output_rows"],
                  "k_min": result.report["k_anonymity"].get("min_k")}, client_ip(request))
    db.commit()
    flash(request, f"가명처리 완료 - 적정성 자동판정: {result.report['verdict']}",
          "success" if result.report["verdict"] != "부적정" else "error")
    return RedirectResponse(f"/deid/{jid}#result", status_code=303)


def col_index(job: DeidJob, name: str) -> int:
    for i, c in enumerate(job.analysis["columns"]):
        if c["name"] == name:
            return i
    return -1


@router.get("/deid/{jid}/report")
def deid_report(request: Request, jid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = _job(db, jid)
    _check_download(job, user, report_only=True)
    return render(request, "print/deid_report.html", db, user, job=job, actions=ACTIONS)


@router.post("/deid/{jid}/release", dependencies=[Depends(verify_csrf)])
def deid_release(request: Request, jid: int, user: User = Depends(steward), db: Session = Depends(get_db)):
    job = _job(db, jid)
    p = job.protocol
    if job.status not in ("processed", "released") or not job.output_path:
        flash(request, "가명처리가 완료된 작업만 제공할 수 있습니다.", "error")
    elif p is None:
        flash(request, "IRB 과제와 연결되지 않은 작업은 연구자에게 제공할 수 없습니다.", "error")
    elif not p.is_active_approval:
        flash(request, f"과제 {p.protocol_no}의 IRB 승인이 유효하지 않아 제공할 수 없습니다.", "error")
    elif job.report.get("verdict") == "부적정":
        flash(request, "적정성 판정이 '부적정'인 데이터는 제공할 수 없습니다. 처리 기준을 조정하십시오.", "error")
    elif job.drb_status != "approved":
        flash(request, "데이터심의위원회(DRB)의 적정성 검토 승인이 필요합니다.", "error")
    elif gate := records.transfer_gate(db, p):
        flash(request, gate, "error")
    else:
        job.status = "released"
        job.released_to = p.pi_id
        job.released_at = now()
        audit.record(db, user, "deid_release", "deid_job", job.id,
                     {"protocol": p.protocol_no, "to": p.pi_id, "sha256": job.report.get("output_sha256")}, client_ip(request))
        notify(db, p.member_ids(), f"{p.protocol_no} 가명정보가 제공되었습니다 ({job.report.get('output_rows', 0):,}행)",
               f"/deid/{job.id}/download")
        db.commit()
        flash(request, f"{p.protocol_no} 연구팀에 가명정보를 제공했습니다.", "success")
    return RedirectResponse(f"/deid/{jid}", status_code=303)


def _check_download(job: DeidJob, user: User, report_only: bool = False) -> None:
    if user.role in (Role.DATA_STEWARD, Role.ADMIN):
        return
    if report_only and user.role in DRB_ROLES:
        return
    p = job.protocol
    if job.status != "released" or p is None or user.id not in p.member_ids():
        raise HTTPException(403, "제공된 가명정보만 해당 연구팀이 다운로드할 수 있습니다.")
    if not p.is_active_approval:
        raise HTTPException(403, "IRB 승인 기간이 유효하지 않아 다운로드할 수 없습니다.")


@router.get("/deid/{jid}/download")
def deid_download(request: Request, jid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = _job(db, jid)
    _check_download(job, user)
    if not job.output_path or not Path(job.output_path).exists():
        raise HTTPException(404, "처리 결과 파일이 없습니다.")
    audit.record(db, user, "deid_download", "deid_job", job.id, {"sha256": job.report.get("output_sha256")}, client_ip(request))
    db.commit()
    stem = Path(job.original_filename).stem
    return Response(
        Path(job.output_path).read_bytes(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(stem)}_pseudonymized.csv"},
    )


@router.post("/deid/{jid}/purge-input", dependencies=[Depends(verify_csrf)])
def deid_purge(request: Request, jid: int, user: User = Depends(steward), db: Session = Depends(get_db)):
    job = _job(db, jid)
    if job.input_path and Path(job.input_path).exists():
        Path(job.input_path).unlink()
        job.input_path = ""
        audit.record(db, user, "deid_delete_input", "deid_job", job.id, {"sha256": job.input_sha256}, client_ip(request))
        db.commit()
        flash(request, "원본 파일을 파기했습니다(파기 기록이 감사 로그에 남았습니다).", "success")
    return RedirectResponse(f"/deid/{jid}", status_code=303)


def _quote(s: str) -> str:
    from urllib.parse import quote

    return quote(s)


# --------------------------------------------------------------------- DRB 적정성 검토
@router.get("/drb")
def drb_queue(request: Request, user: User = Depends(drb_member), db: Session = Depends(get_db)):
    jobs = db.scalars(select(DeidJob).where(DeidJob.status.in_(["processed", "released"])).order_by(DeidJob.id.desc())).all()
    return render(request, "deid/drb.html", db, user, jobs=jobs)


@router.post("/deid/{jid}/drb", dependencies=[Depends(verify_csrf)])
async def drb_decide(request: Request, jid: int, user: User = Depends(drb_member), db: Session = Depends(get_db)):
    job = _job(db, jid)
    form = await request.form()
    try:
        records.drb_review(db, user, job, form.get("decision") == "approve", str(form.get("comment", "")), client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/drb", status_code=303)
    db.commit()
    flash(request, f"가명처리 #{job.id} 적정성 검토를 등록했습니다.", "success")
    return RedirectResponse("/drb", status_code=303)
