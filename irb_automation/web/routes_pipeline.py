"""문서 자동화 파이프라인 화면.

/protocols/import                 타 기관 서식 연구계획서 가져오기(업로드 → 분석 → 선택 적용 → 새 과제)
/protocols/{pid}/pipeline         과제의 분석 이력 · 재분석 · 추출 제안 적용
/pipeline/{run_id}                단계별 진행 · 결과
/api/pipeline/{run_id}            진행 상태 JSON(pipeline.js 폴링)
/pipeline/{run_id}/markdown|pdf   변환 결과 내려받기
/admin/ai                         AI 상태 · 정책 · 최근 실행 · 전송 기록
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..ai import client as ai
from ..audit import ACTION_LABELS
from ..config import get_settings
from ..database import get_db
from ..docproc import extract_fields as ef
from ..docproc.convert import safe_name, text_to_pdf, to_pdf
from ..docproc.office import soffice_available
from ..models import AuditLog, Protocol, Role, User
from ..pipeline import service as ps
from ..pipeline.models import ExtractedTextCache, PipelineRun
from ..workflow import service as wf
from .deps import client_ip, current_user, flash, render, require_roles, verify_csrf

router = APIRouter()

IMPORT_EXT = {".pdf", ".docx", ".hwp", ".hwpx", ".doc", ".rtf", ".odt", ".txt", ".md", ".html", ".htm",
              ".png", ".jpg", ".jpeg"}
researcher_or_staff = require_roles(Role.RESEARCHER)


def _run(db: Session, user: User, run_id: int) -> PipelineRun:
    run = db.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(404, "분석 실행을 찾을 수 없습니다.")
    if not ps.can_view_run(db, user, run):
        raise HTTPException(403, "이 분석 결과를 볼 권한이 없습니다.")
    return run


def _protocol(db: Session, user: User, pid: int) -> Protocol:
    p = db.get(Protocol, pid)
    if p is None:
        raise HTTPException(404, "과제를 찾을 수 없습니다.")
    if not wf.can_view(db, user, p):
        raise HTTPException(403, "이 과제를 볼 권한이 없습니다.")
    return p


def _ctx_common() -> dict:
    return {"step_labels": ps.STEP_LABELS, "status_labels": ps.STATUS_LABELS, "kind_labels": ps.KIND_LABELS,
            "ai_on": ai.ai_enabled()}


# ---------------------------------------------------------------------- 가져오기
@router.get("/protocols/import")
def import_form(request: Request, user: User = Depends(researcher_or_staff), db: Session = Depends(get_db)):
    recent = list(db.scalars(select(PipelineRun).where(PipelineRun.kind == "import", PipelineRun.created_by == user.id)
                             .order_by(PipelineRun.id.desc()).limit(10)))
    return render(request, "pipeline/import.html", db, user, recent=recent, exts=sorted(IMPORT_EXT), **_ctx_common())


@router.post("/protocols/import", dependencies=[Depends(verify_csrf)])
async def import_upload(request: Request, user: User = Depends(researcher_or_staff), db: Session = Depends(get_db)):
    form = await request.form()
    upload = form.get("file")
    if not hasattr(upload, "read") or not getattr(upload, "filename", ""):
        flash(request, "연구계획서 파일을 선택하십시오.", "error")
        return RedirectResponse("/protocols/import", 303)
    filename = Path(upload.filename).name
    if Path(filename).suffix.lower() not in IMPORT_EXT:
        flash(request, f"지원하지 않는 파일 형식입니다({Path(filename).suffix}).", "error")
        return RedirectResponse("/protocols/import", 303)
    raw = await upload.read()
    if not raw or len(raw) > get_settings().max_upload_mb * 1024 * 1024:
        flash(request, "빈 파일이거나 크기 제한을 초과했습니다.", "error")
        return RedirectResponse("/protocols/import", 303)
    run = ps.start_import_pipeline(db, user, filename, raw)
    db.commit()
    return RedirectResponse(f"/pipeline/{run.id}", 303)


@router.post("/pipeline/{run_id}/apply-import", dependencies=[Depends(verify_csrf)])
async def apply_import(request: Request, run_id: int, user: User = Depends(researcher_or_staff),
                       db: Session = Depends(get_db)):
    run = _run(db, user, run_id)
    if run.created_by != user.id:
        raise HTTPException(403, "가져오기를 실행한 사용자만 과제를 만들 수 있습니다.")
    form = await request.form()
    selected = {str(v) for v in form.getlist("fields")}
    try:
        protocol = ps.apply_import(db, user, run, selected, client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/pipeline/{run.id}", 303)
    db.commit()
    flash(request, f"새 과제(작성중)를 만들고 원본 계획서를 첨부했습니다. 반영 항목 {len(selected)}개 - 내용을 확인·보완하십시오.",
          "success")
    return RedirectResponse(f"/protocols/{protocol.id}/edit", 303)


# ---------------------------------------------------------------------- 과제별
@router.get("/protocols/{pid}/pipeline")
def protocol_runs(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    runs = ps.runs_for_protocol(db, p.id)
    latest = next((r for r in runs if ps.latest_fields(r)), None)
    rows = _field_rows(latest, p) if latest else []
    can_apply = wf.is_member(user, p) and p.is_editable
    return render(request, "pipeline/protocol.html", db, user, p=p, runs=runs, latest=latest, rows=rows,
                  can_apply=can_apply, can_run=wf.is_member(user, p) or user.is_admin, **_ctx_common())


@router.post("/protocols/{pid}/pipeline", dependencies=[Depends(verify_csrf)])
def protocol_rerun(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    if not (wf.is_member(user, p) or user.is_admin):
        raise HTTPException(403, "재분석 권한이 없습니다.")
    run = ps.start_protocol_pipeline(db, user, p.id)
    db.commit()
    return RedirectResponse(f"/pipeline/{run.id}", 303)


@router.post("/protocols/{pid}/pipeline/{run_id}/apply", dependencies=[Depends(verify_csrf)])
async def protocol_apply(request: Request, pid: int, run_id: int, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    run = _run(db, user, run_id)
    if run.protocol_id != p.id:
        raise HTTPException(400, "이 과제의 분석 결과가 아닙니다.")
    form = await request.form()
    selected = {str(v) for v in form.getlist("fields")}
    mode = "overwrite" if form.get("mode") == "overwrite" else "fill"
    try:
        applied = ps.apply_to_protocol(db, user, p, run, selected, mode, client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/protocols/{p.id}/pipeline", 303)
    db.commit()
    flash(request, f"{len(applied)}개 항목을 반영했습니다." if applied else "반영할 항목이 없었습니다(빈 칸만 채우기).",
          "success" if applied else "info")
    return RedirectResponse(f"/protocols/{p.id}/pipeline", 303)


VALUE_CATALOGS = {"study_type": "study_types", "deid_level": "deid_levels", "data_sources": "data_sources",
                  "identifiers_collected": "identifiers", "sensitive_categories": "sensitive_categories",
                  "vulnerable_populations": "vulnerable_populations", "unstructured_data": "unstructured_data",
                  "waiver_impracticable_reasons": "waiver_reasons"}
CONSENT_LABELS = {"obtain": "동의 취득", "waiver": "동의 면제"}


def _labelled(key: str, value):
    from ..engine.ruleset import get_ruleset

    if key == "consent_plan":
        return CONSENT_LABELS.get(value, value)
    catalog = VALUE_CATALOGS.get(key)
    if not catalog or value in (None, ""):
        return value
    rs = get_ruleset()
    if isinstance(value, (list, tuple)):
        return [rs.label(catalog, v) for v in value]
    return rs.label(catalog, value)


def _field_rows(run: PipelineRun, protocol: Protocol | None = None) -> list[dict]:
    rows = ef.labeled(ps.latest_fields(run))
    rs_fields = set(ps.PROTOCOL_FIELDS)
    for row in rows:
        row["display"] = _display(_labelled(row["key"], row["value"]))
        if protocol is not None:
            current = (protocol.custom_fields or {}).get(row["key"][7:]) if row["key"].startswith("custom:") and \
                hasattr(protocol, "custom_fields") else getattr(protocol, row["key"], None)
            row["current"] = _display(_labelled(row["key"], current))
            row["same"] = row["current"] == row["display"]
        row["applicable"] = row["key"] in rs_fields or row["key"].startswith("custom:")
    return rows


def _display(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


# ---------------------------------------------------------------------- 실행 결과
@router.get("/pipeline/{run_id}")
def run_detail(request: Request, run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = _run(db, user, run_id)
    protocol = db.get(Protocol, run.protocol_id) if run.protocol_id else None
    rows = _field_rows(run, protocol if run.kind != "import" else None)
    return render(request, "pipeline/run.html", db, user, run=run, protocol=protocol, rows=rows,
                  steps={s.name: s for s in run.steps}, can_create=(run.kind == "import" and run.created_by == user.id
                                                                    and not run.protocol_id and not run.active),
                  **_ctx_common())


@router.get("/api/pipeline/{run_id}")
def run_status(run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return JSONResponse(ps.run_payload(_run(db, user, run_id)))


def _text_for(db: Session, run: PipelineRun) -> str:
    if run.input_sha256:
        cached = db.scalars(select(ExtractedTextCache).where(ExtractedTextCache.sha256 == run.input_sha256)).first()
        if cached:
            return cached.text
    return ""


@router.get("/pipeline/{run_id}/markdown")
def run_markdown(run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = _run(db, user, run_id)
    step = run.step("convert")
    path = Path((step.result or {}).get("markdown_path", "")) if step else Path("")
    if not step or step.status != "succeeded" or not path.is_file():
        raise HTTPException(404, "변환 결과가 없습니다.")
    name = Path(safe_name(run.input_name)).stem + ".md"
    return Response(path.read_bytes(), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(name)}"})


@router.get("/pipeline/{run_id}/pdf")
def run_pdf(run_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = _run(db, user, run_id)
    pdf = None
    if run.input_path and Path(run.input_path).is_file():
        pdf = to_pdf(run.input_name, Path(run.input_path).read_bytes())
    if pdf is None and (text := _text_for(db, run)):
        pdf = text_to_pdf(text, run.input_name)
    if pdf is None:
        raise HTTPException(404, "PDF로 변환할 수 없습니다.")
    name = Path(safe_name(run.input_name)).stem + ".pdf"
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{_quote(name)}"})


def _quote(name: str) -> str:
    from urllib.parse import quote

    return quote(name)


# ---------------------------------------------------------------------- 관리
@router.get("/admin/ai")
def admin_ai(request: Request, user: User = Depends(require_roles(Role.ADMIN)),
             db: Session = Depends(get_db)):
    s = get_settings()
    runs = list(db.scalars(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(30)))
    counts = dict(db.execute(select(PipelineRun.status, func.count(PipelineRun.id)).group_by(PipelineRun.status)).all())
    transmissions = list(db.scalars(select(AuditLog).where(AuditLog.action == "ai_transmit")
                                    .order_by(AuditLog.id.desc()).limit(30)))
    by_purpose: dict[str, int] = {}
    for t in db.scalars(select(AuditLog).where(AuditLog.action == "ai_transmit")):
        key = (t.details or {}).get("purpose", "-")
        by_purpose[key] = by_purpose.get(key, 0) + 1
    try:
        import anthropic  # noqa: F401

        sdk = True
    except ImportError:
        sdk = False
    status = {
        "enabled": s.ai_enabled, "available": ai.ai_enabled(), "sdk": sdk, "model": s.ai_model,
        "auto": s.ai_pipeline_auto, "allowed": s.ai_doc_types_allowed, "soffice": soffice_available(),
        "soffice_path": s.soffice_path, "sync": s.pipeline_sync,
    }
    return render(request, "pipeline/admin.html", db, user, status=status, runs=runs, counts=counts,
                  transmissions=transmissions, by_purpose=by_purpose, usage=ai.recent_usage()[:20],
                  action_labels=ACTION_LABELS, **_ctx_common())
