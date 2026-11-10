"""가명처리 도구(연구자용): 추출 자료의 개인정보 탐지·가명처리·적정성 자동평가, 처리 보고서를 IRB 서류 근거로 활용."""
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
from ..models import DeidJob, Protocol, Role, User
from ..timeutil import now
from ..workflow import service as wf
from .deps import client_ip, current_user, flash, render, require_roles, verify_csrf

router = APIRouter()
researcher = require_roles(Role.RESEARCHER)


def _job(db: Session, jid: int, user: User) -> DeidJob:
    job = db.get(DeidJob, jid)
    if job is None or not (user.is_admin or job.created_by == user.id
                           or (job.protocol and user.id in job.protocol.member_ids())):
        raise HTTPException(404)
    return job


def _salt(job: DeidJob) -> str:
    # 과제별로 다른 가명이 생성되도록 과제 ID를 솔트로 사용(과제 간 결합 방지)
    return f"protocol:{job.protocol_id}" if job.protocol_id else f"job:{job.id}"


@router.get("/deid")
def deid_list(request: Request, user: User = Depends(researcher), db: Session = Depends(get_db)):
    jobs = [j for j in db.scalars(select(DeidJob).order_by(DeidJob.id.desc()))
            if user.is_admin or j.created_by == user.id or (j.protocol and user.id in j.protocol.member_ids())]
    return render(request, "deid/list.html", db, user, jobs=jobs)


@router.get("/deid/new")
def deid_new(request: Request, protocol_id: int | None = None, user: User = Depends(researcher), db: Session = Depends(get_db)):
    protocols = [p for p in db.scalars(select(Protocol)) if wf.can_view(db, user, p)]
    return render(request, "deid/new.html", db, user, protocols=protocols, selected=protocol_id)


@router.post("/deid/new", dependencies=[Depends(verify_csrf)])
async def deid_upload(request: Request, user: User = Depends(researcher), db: Session = Depends(get_db)):
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
    if protocol is not None and not wf.can_view(db, user, protocol):
        protocol = None
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
def deid_detail(request: Request, jid: int, user: User = Depends(researcher), db: Session = Depends(get_db)):
    job = _job(db, jid, user)
    return render(request, "deid/detail.html", db, user, job=job, actions=ACTIONS,
                  policy=get_ruleset().deid_policy)


@router.post("/deid/{jid}/process", dependencies=[Depends(verify_csrf)])
async def deid_process(request: Request, jid: int, user: User = Depends(researcher), db: Session = Depends(get_db)):
    job = _job(db, jid, user)
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
    job = _job(db, jid, user)
    return render(request, "print/deid_report.html", db, user, job=job, actions=ACTIONS)


@router.get("/deid/{jid}/download")
def deid_download(request: Request, jid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    job = _job(db, jid, user)
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
def deid_purge(request: Request, jid: int, user: User = Depends(researcher), db: Session = Depends(get_db)):
    job = _job(db, jid, user)
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
