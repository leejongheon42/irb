"""기관 서식(DOCX/HWPX) 관리와 서식 자료실 화면."""
from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..doctemplates import service
from ..doctemplates.fields import available_fields, is_known_path
from ..doctemplates.mapping import suggest_all
from ..doctemplates.models import DocumentTemplate
from ..doctemplates.samples import create_samples
from ..engine.ruleset import get_ruleset
from ..models import Protocol, Role, User
from ..workflow import service as wf
from .deps import client_ip, current_user, flash, render, require_roles, verify_csrf

router = APIRouter()
template_admin = require_roles(Role.COORDINATOR)  # 시스템관리자는 require_roles가 항상 허용

KIND_LABELS = {"field": "값", "compare": "비교(체크)", "list": "목록(반복)"}


def _template(db: Session, tid: int) -> DocumentTemplate:
    tpl = db.get(DocumentTemplate, tid)
    if tpl is None:
        raise HTTPException(404, "서식을 찾을 수 없습니다.")
    return tpl


def _download(content: bytes, filename: str, media_type: str) -> Response:
    return Response(content, media_type=media_type,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})


def _grouped_fields(db: Session) -> "OrderedDict[str, list]":
    groups: OrderedDict[str, list] = OrderedDict()
    for f in available_fields(db):
        groups.setdefault(f.group, []).append(f)
    return groups


# ===================================================================== 관리
@router.get("/admin/templates")
def template_list(request: Request, user: User = Depends(template_admin), db: Session = Depends(get_db)):
    rs = get_ruleset()
    rows = db.scalars(select(DocumentTemplate).order_by(DocumentTemplate.doc_type, DocumentTemplate.version.desc())).all()
    by_type: OrderedDict[str, list[DocumentTemplate]] = OrderedDict()
    for dt in rs.catalog("document_types"):
        items = [t for t in rows if t.doc_type == dt]
        if items:
            by_type[dt] = items
    return render(request, "doctemplates/list.html", db, user, by_type=by_type,
                  doc_types=rs.options("document_types"), unmapped=service.unmapped)


@router.post("/admin/templates", dependencies=[Depends(verify_csrf)])
async def template_upload(request: Request, user: User = Depends(template_admin), db: Session = Depends(get_db)):
    form = await request.form()
    upload = form.get("file")
    if not hasattr(upload, "read") or not getattr(upload, "filename", ""):
        flash(request, "서식 파일(DOCX 또는 HWPX)을 선택하십시오.", "error")
        return RedirectResponse("/admin/templates", status_code=303)
    raw = await upload.read()
    try:
        tpl = service.upload_template(
            db, user, filename=upload.filename, raw=raw, doc_type=str(form.get("doc_type", "")),
            name=str(form.get("name", "")), notes=str(form.get("notes", "")), ip=client_ip(request),
        )
    except wf.WorkflowError as e:
        db.rollback()
        flash(request, str(e), "error")
        return RedirectResponse("/admin/templates", status_code=303)
    db.commit()
    missing = service.unmapped(tpl)
    flash(request, f"'{tpl.name}' v{tpl.version}을(를) 등록했습니다. 자리표시자 {len(tpl.placeholders)}개 중 "
                   f"{len(tpl.placeholders) - len(missing)}개를 자동 매핑했습니다. 확인 후 활성화하십시오.",
          "success" if not missing else "info")
    return RedirectResponse(f"/admin/templates/{tpl.id}", status_code=303)


@router.post("/admin/templates/samples", dependencies=[Depends(verify_csrf)])
def template_samples(request: Request, user: User = Depends(template_admin), db: Session = Depends(get_db)):
    created = create_samples(db, user, activate=False, ip=client_ip(request))
    db.commit()
    flash(request, f"샘플 서식 {len(created)}종(서로 다른 병원 양식)을 등록했습니다. 매핑을 확인한 뒤 활성화하십시오.", "success")
    return RedirectResponse("/admin/templates", status_code=303)


@router.get("/admin/templates/{tid}")
def template_detail(request: Request, tid: int, user: User = Depends(template_admin), db: Session = Depends(get_db)):
    tpl = _template(db, tid)
    fields = available_fields(db)
    suggestions = suggest_all(tpl.placeholders or [], fields)
    rows = []
    for i, ph in enumerate(tpl.placeholders or []):
        target = (tpl.mapping or {}).get(ph["name"], "")
        path, _, fmt = target.partition("|")
        direct = not target and is_known_path(ph["name"])
        rows.append({
            "i": i, "ph": ph, "path": path or (ph["name"] if direct else ""), "fmt": fmt,
            "status": "mapped" if target else ("direct" if direct else "missing"),
            "suggestion": suggestions.get(ph["name"]),
        })
    try:
        preview_text = service.plain_text(tpl)[:3000]
    except wf.WorkflowError:
        preview_text = ""
    protocols = db.scalars(select(Protocol).order_by(Protocol.updated_at.desc()).limit(200)).all()
    return render(request, "doctemplates/detail.html", db, user, tpl=tpl, rows=rows, groups=_grouped_fields(db),
                  kind_labels=KIND_LABELS, missing=service.unmapped(tpl), versions=service.versions(db, tpl.doc_type),
                  preview_text=preview_text, protocols=protocols, pdf_available=service.pdf_converter() is not None)


@router.post("/admin/templates/{tid}/mapping", dependencies=[Depends(verify_csrf)])
async def template_mapping(request: Request, tid: int, user: User = Depends(template_admin), db: Session = Depends(get_db)):
    tpl = _template(db, tid)
    form = await request.form()
    mapping: dict[str, str] = {}
    for key in form:
        if not key.startswith("ph_"):
            continue
        idx = key[3:]
        name = str(form.get(key, ""))
        path = str(form.get(f"map_{idx}", "")).strip()
        fmt = str(form.get(f"fmt_{idx}", "")).strip()
        if name and path:
            mapping[name] = f"{path}|{fmt}" if fmt else path
    try:
        missing = service.save_mapping(db, user, tpl, mapping, client_ip(request))
        if form.get("activate") == "1":
            service.activate(db, user, tpl, client_ip(request))
    except wf.WorkflowError as e:
        db.rollback()
        flash(request, str(e), "error")
        return RedirectResponse(f"/admin/templates/{tid}", status_code=303)
    db.commit()
    if tpl.active:
        flash(request, f"매핑을 저장하고 활성화했습니다. 이제 '{get_ruleset().label('document_types', tpl.doc_type)}' "
                       "자동 작성은 이 서식을 사용합니다.", "success")
    else:
        flash(request, "매핑을 저장했습니다." + (f" 미매핑 {len(missing)}개가 남아 있습니다." if missing else ""),
              "success" if not missing else "info")
    return RedirectResponse(f"/admin/templates/{tid}", status_code=303)


@router.post("/admin/templates/{tid}/activate", dependencies=[Depends(verify_csrf)])
def template_activate(request: Request, tid: int, user: User = Depends(template_admin), db: Session = Depends(get_db)):
    tpl = _template(db, tid)
    try:
        service.activate(db, user, tpl, client_ip(request))
    except wf.WorkflowError as e:
        db.rollback()
        flash(request, str(e), "error")
        return RedirectResponse(f"/admin/templates/{tid}", status_code=303)
    db.commit()
    flash(request, f"v{tpl.version}을(를) 활성화했습니다(같은 서류 종류의 다른 버전은 비활성화).", "success")
    return RedirectResponse(f"/admin/templates/{tid}", status_code=303)


@router.post("/admin/templates/{tid}/deactivate", dependencies=[Depends(verify_csrf)])
def template_deactivate(request: Request, tid: int, user: User = Depends(template_admin), db: Session = Depends(get_db)):
    tpl = _template(db, tid)
    service.deactivate(db, user, tpl, client_ip(request))
    db.commit()
    flash(request, "비활성화했습니다. 이 서류 종류는 기본 서식으로 작성됩니다.", "info")
    return RedirectResponse(f"/admin/templates/{tid}", status_code=303)


@router.get("/admin/templates/{tid}/file")
def template_file(request: Request, tid: int, user: User = Depends(template_admin), db: Session = Depends(get_db)):
    tpl = _template(db, tid)
    try:
        raw = service.read_original(tpl)
    except wf.WorkflowError as e:
        raise HTTPException(404, str(e)) from e
    audit.record(db, user, "template_download", "doc_template", tpl.id, {"version": tpl.version}, client_ip(request))
    db.commit()
    return _download(raw, tpl.original_filename, tpl.media_type)


@router.get("/admin/templates/{tid}/fill")
def template_fill(request: Request, tid: int, protocol_id: int, fmt: str = "",
                  user: User = Depends(template_admin), db: Session = Depends(get_db)):
    tpl = _template(db, tid)
    return _fill_response(request, db, user, tpl, protocol_id, fmt)


def _fill_response(request: Request, db: Session, user: User, tpl: DocumentTemplate, protocol_id: int, fmt: str):
    p = db.get(Protocol, protocol_id)
    if p is None:
        raise HTTPException(404, "과제를 찾을 수 없습니다.")
    if not wf.can_view(db, user, p):
        raise HTTPException(403, "이 과제를 열람할 권한이 없습니다.")
    try:
        result = service.fill(db, tpl, p)
    except wf.WorkflowError as e:
        raise HTTPException(400, str(e)) from e
    content, filename, media_type = result.content, result.filename, result.media_type
    if fmt == "pdf":
        pdf = service.to_pdf(filename, content)
        if pdf is None:
            raise HTTPException(400, "PDF 변환을 사용할 수 없습니다(변환기 미설치).")
        content, filename, media_type = pdf, Path(filename).stem + ".pdf", "application/pdf"
    audit.record(db, user, "template_fill", "doc_template", tpl.id,
                 {"protocol_id": p.id, "fmt": fmt or tpl.fmt, "missing": result.missing}, client_ip(request))
    db.commit()
    return _download(content, filename, media_type)


# ===================================================================== 서식 자료실(전체 사용자)
@router.get("/doc-templates")
def template_library(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    mine = [p for p in db.scalars(select(Protocol).order_by(Protocol.updated_at.desc())) if user.id in p.member_ids()]
    return render(request, "doctemplates/library.html", db, user, templates_=service.active_templates(db),
                  protocols=mine, pdf_available=service.pdf_converter() is not None)


def _active(db: Session, tid: int) -> DocumentTemplate:
    tpl = _template(db, tid)
    if not tpl.active:
        raise HTTPException(404, "사용 중인 서식이 아닙니다.")
    return tpl


@router.get("/doc-templates/{tid}/file")
def library_file(request: Request, tid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    tpl = _active(db, tid)
    try:
        raw = service.read_original(tpl)
    except wf.WorkflowError as e:
        raise HTTPException(404, str(e)) from e
    return _download(raw, tpl.original_filename, tpl.media_type)


@router.get("/doc-templates/{tid}/fill")
def library_fill(request: Request, tid: int, protocol_id: int, fmt: str = "",
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    tpl = _active(db, tid)
    return _fill_response(request, db, user, tpl, protocol_id, fmt)
