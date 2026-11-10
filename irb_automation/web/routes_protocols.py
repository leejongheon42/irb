"""연구과제: 작성 → 서류 자동 완성·점검 → IRB 제출 기록 → 보완 대응 → 결과 기록 화면."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..database import get_db
from ..doctemplates.service import to_pdf as tpl_to_pdf
from ..documents import GENERATABLE, build_document, generatable_doc_types, generate_and_attach
from ..engine.ai_review import run_ai_review
from ..engine.facts import ProtocolFacts, snapshot_protocol
from ..engine.ruleset import get_ruleset
from ..models import (
    SUBMISSION_RESULTS,
    Document,
    Protocol,
    ProtocolStatus,
    Role,
    SubmissionKind,
    User,
)
from ..submission import service as submission_service
from ..submission.guess import guess_doc_type
from ..timeutil import parse_date
from ..workflow import service as wf
from ..workflow.diff import diff_snapshots, previous_snapshot
from .deps import client_ip, current_user, flash, render, verify_csrf
from .forms import parse_protocol_form

log = logging.getLogger(__name__)
router = APIRouter(prefix="")
S = ProtocolStatus

ALLOWED_EXT = {".pdf", ".doc", ".docx", ".hwp", ".hwpx", ".xls", ".xlsx", ".csv", ".txt", ".jpg", ".jpeg", ".png", ".zip"}


def _get(db: Session, user: User, pid: int) -> Protocol:
    p = db.get(Protocol, pid)
    if p is None:
        raise HTTPException(404, "과제를 찾을 수 없습니다.")
    if not wf.can_view(db, user, p):
        raise HTTPException(403, "이 과제를 열람할 권한이 없습니다.")
    return p


def _back(pid: int, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/protocols/{pid}{anchor}", status_code=303)


def _users(db: Session, *roles: Role) -> list[User]:
    q = select(User).where(User.is_active.is_(True)).order_by(User.full_name)
    if roles:
        q = q.where(User.role.in_(roles))
    return list(db.scalars(q))


# --------------------------------------------------------------------- 목록/생성
@router.get("/protocols")
def list_protocols(request: Request, status: str = "", q: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    items = [p for p in db.scalars(select(Protocol).order_by(Protocol.updated_at.desc())) if wf.can_view(db, user, p)]
    if status:
        items = [p for p in items if p.status.value == status]
    if q:
        ql = q.lower()
        items = [p for p in items if ql in p.title.lower() or ql in (p.protocol_no or "").lower() or ql in p.pi.full_name.lower()]
    return render(request, "protocols/list.html", db, user, items=items, status=status, q=q)


@router.get("/protocols/new")
def new_protocol_form(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role not in (Role.RESEARCHER, Role.ADMIN):
        raise HTTPException(403, "연구자만 과제를 생성할 수 있습니다.")
    return render(request, "protocols/form.html", db, user, protocol=None, v=ProtocolFacts(), researchers=_users(db, Role.RESEARCHER))


@router.post("/protocols/new", dependencies=[Depends(verify_csrf)])
async def create_protocol(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    data = parse_protocol_form(await request.form())
    data["co_investigators"] = [i for i in data.get("co_investigators", []) if i != user.id]
    try:
        p = wf.create_protocol(db, user, data, client_ip(request))
    except wf.WorkflowError as e:
        raise HTTPException(403, str(e)) from e
    db.commit()
    flash(request, "과제가 생성되었습니다. '제출 서류 패키지'에서 필요한 서류를 자동으로 완성하십시오.", "success")
    return RedirectResponse(f"/protocols/{p.id}/package", status_code=303)


# --------------------------------------------------------------------- 상세/수정
def _knowledge_context(db: Session, user: User, p, preview, sub) -> tuple[dict, list]:
    """사전검토 의견별 관련 근거와 유사 과제(선례). 지식 모듈 오류가 화면을 막지 않도록 격리."""
    result = preview or (sub.latest_prereview.result if sub and sub.latest_prereview else None)
    try:
        from ..knowledge import service as ks

        related = ks.related_for_findings(db, result.get("findings", [])) if result else {}
        similar = ks.similar_protocols(db, p, user=user)
    except Exception:  # pragma: no cover - 보조 정보
        log.exception("knowledge context failed for protocol %s", p.id)
        return {}, []
    return related, similar


@router.get("/protocols/{pid}")
def detail(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    preview = wf.preview(db, p) if p.is_editable else None
    sub = p.current_submission
    co_names = [db.get(User, uid) for uid in p.co_investigators or []]
    from ..models import AuditLog

    logs = db.scalars(
        select(AuditLog).where(AuditLog.entity_type == "protocol", AuditLog.entity_id == p.id).order_by(AuditLog.id.desc())
    ).all()
    diff = None
    if sub and sub.kind.value in ("resubmission", "amendment"):
        prev = previous_snapshot(p, sub)
        diff = diff_snapshots(prev, sub.snapshot, get_ruleset()) if prev else None
    revision_items = wf.open_revision_items(db, p) if p.status == S.REVISION_REQUESTED else []
    related, similar = _knowledge_context(db, user, p, preview, sub)
    result_options = []
    if sub and not sub.closed and p.status == S.SUBMITTED:
        result_options = [(k, SUBMISSION_RESULTS[k]) for k in wf.RESULTS_BY_KIND[sub.kind]]
    return render(
        request, "protocols/detail.html", db, user,
        p=p, sub=sub, preview=preview, related=related, similar=similar,
        diff=diff, revision_items=revision_items, generatable=generatable_doc_types(db),
        co_names=[u for u in co_names if u], is_member=wf.is_member(user, p), logs=logs,
        result_options=result_options, ai_enabled=get_settings().ai_review_enabled,
    )


@router.get("/protocols/{pid}/edit")
def edit_form(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    if not (wf.is_member(user, p) and p.is_editable):
        flash(request, "현재 상태에서는 수정할 수 없습니다.", "error")
        return _back(pid)
    return render(request, "protocols/form.html", db, user, protocol=p, v=p, researchers=_users(db, Role.RESEARCHER))


@router.post("/protocols/{pid}/edit", dependencies=[Depends(verify_csrf)])
async def edit(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    data = parse_protocol_form(await request.form())
    data["co_investigators"] = [i for i in data.get("co_investigators", []) if i != p.pi_id]
    try:
        wf.update_protocol(db, user, p, data, client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return _back(pid)
    db.commit()
    flash(request, "저장되었습니다. 자동 사전점검 결과를 확인하십시오.", "success")
    return _back(pid, "#precheck")


# --------------------------------------------------------------------- 서류
@router.post("/protocols/{pid}/documents", dependencies=[Depends(verify_csrf)])
async def upload_document(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    if not (wf.is_member(user, p) or user.is_admin):
        raise HTTPException(403, "서류 업로드 권한이 없습니다.")
    form = await request.form()
    doc_type = str(form.get("doc_type", "other"))
    upload = form.get("file")
    if not hasattr(upload, "read") or not getattr(upload, "filename", ""):
        flash(request, "파일을 선택하십시오.", "error")
        return _back(pid, "#documents")
    filename = Path(upload.filename).name
    content = await upload.read()
    if doc_type not in get_ruleset().catalog("document_types"):
        g = guess_doc_type(filename, content)
        doc_type = g.doc_type if g.confidence >= 0.5 else "other"
    try:
        doc, result = submission_service.store_document(
            db, user, p, doc_type=doc_type, filename=filename, raw=content, ip=client_ip(request))
    except submission_service.SubmissionError as e:
        db.commit()  # 거부 기록(감사 로그)은 남긴다
        flash(request, f"제출 거부: {e}", "error")
        return _back(pid, "#documents")
    db.commit()
    flash(request, f"'{filename}' 업로드 완료 (v{doc.version}).", "success")
    for w in result.warnings:
        flash(request, f"확인 필요: {w}", "info")
    return _back(pid, "#documents")


@router.get("/documents/{doc_id}")
def download_document(request: Request, doc_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404)
    _get(db, user, doc.protocol_id)
    if not doc.stored_path or not Path(doc.stored_path).is_file():
        raise HTTPException(404, "저장된 파일이 없습니다(데모 데이터이거나 파일이 이동됨).")
    audit.record(db, user, "document_download", "protocol", doc.protocol_id, {"doc_id": doc.id}, client_ip(request))
    db.commit()
    return FileResponse(doc.stored_path, filename=doc.filename)


# --------------------------------------------------------------------- 사전점검/제출
@router.get("/protocols/{pid}/precheck")
def precheck(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    return render(request, "protocols/precheck.html", db, user, p=p, result=wf.preview(db, p))


@router.post("/protocols/{pid}/submit", dependencies=[Depends(verify_csrf)])
async def submit(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """외부 IRB(e-IRB)에 제출했음을 기록한다."""
    p = _get(db, user, pid)
    form = await request.form()
    try:
        sub = wf.record_submission(
            db, user, p, external_ref=str(form.get("external_ref", "")),
            submitted_on=parse_date(str(form.get("submitted_on", ""))), force=form.get("force") == "on",
            ip=client_ip(request))
    except wf.WorkflowError as e:
        db.rollback()
        flash(request, str(e), "error")
        return _back(pid, "#submit")
    db.commit()
    flash(request, f"{sub.kind.label} 제출을 기록했습니다(예상 심의유형: {sub.review_type.label}). "
                   "IRB 결과나 보완 요청을 받으면 이 화면에 입력하십시오.", "success")
    return _back(pid)


@router.post("/protocols/{pid}/amend", dependencies=[Depends(verify_csrf)])
def amend(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    try:
        wf.start_amendment(db, user, p, client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return _back(pid)
    db.commit()
    flash(request, "변경 내용을 수정한 뒤 변경심의를 제출하십시오. 기존 승인은 변경 승인 시까지 유효합니다.", "info")
    return RedirectResponse(f"/protocols/{pid}/edit", status_code=303)


@router.post("/protocols/{pid}/amend/cancel", dependencies=[Depends(verify_csrf)])
def cancel_amend(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    try:
        wf.cancel_amendment(db, user, p, client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return _back(pid)
    db.commit()
    flash(request, "변경 신청을 취소하고 승인된 내용으로 되돌렸습니다.", "info")
    return _back(pid)


@router.get("/protocols/{pid}/report/{kind}")
def report_form(request: Request, pid: int, kind: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    k = SubmissionKind(kind)
    return render(request, "protocols/report_form.html", db, user, p=p, kind=k)


@router.post("/protocols/{pid}/report/{kind}", dependencies=[Depends(verify_csrf)])
async def report_submit(request: Request, pid: int, kind: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    k = SubmissionKind(kind)
    form = await request.form()
    report = {
        "progress_summary": str(form.get("progress_summary", "")).strip(),
        "results_summary": str(form.get("results_summary", "")).strip(),
        "subjects_analyzed": str(form.get("subjects_analyzed", "")).strip(),
        "deviations": str(form.get("deviations", "")).strip(),
        "data_breach": form.get("data_breach") == "on",
        "breach_detail": str(form.get("breach_detail", "")).strip(),
        "publications": str(form.get("publications", "")).strip(),
        "data_destroyed": form.get("data_destroyed") == "on",
        "destruction_date": str(form.get("destruction_date", "")).strip(),
    }
    try:
        sub = wf.submit_report(db, user, p, k, report, client_ip(request),
                               external_ref=str(form.get("external_ref", "")))
    except wf.WorkflowError as e:
        db.rollback()
        flash(request, str(e), "error")
        return _back(pid)
    db.commit()
    flash(request, f"{sub.kind.label} 제출을 기록했습니다.", "success")
    return _back(pid)


# --------------------------------------------------------------------- IRB 보완 요청·결과 입력
@router.post("/protocols/{pid}/revision-request", dependencies=[Depends(verify_csrf)])
async def revision_request(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """IRB에서 받은 보완 요청문을 붙여넣으면 항목별로 나눠 답변 대상으로 만든다."""
    p = _get(db, user, pid)
    form = await request.form()
    try:
        items = wf.record_revision_request(db, user, p, str(form.get("text", "")), client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return _back(pid)
    db.commit()
    flash(request, f"보완 요청 {len(items)}개 항목을 등록했습니다. 'AI 답변 초안'으로 답변서를 작성하십시오.", "success")
    return RedirectResponse(f"/protocols/{pid}/revision", status_code=303)


@router.post("/protocols/{pid}/result", dependencies=[Depends(verify_csrf)])
async def record_result(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    form = await request.form()
    try:
        sub = wf.record_result(
            db, user, p, str(form.get("result", "")), result_date=parse_date(str(form.get("result_date", ""))),
            approval_end=parse_date(str(form.get("approval_end", ""))), irb_no=str(form.get("irb_no", "")),
            note=str(form.get("note", "")), ip=client_ip(request))
    except wf.WorkflowError as e:
        db.rollback()
        flash(request, str(e), "error")
        return _back(pid)
    db.commit()
    flash(request, f"IRB 결과 '{sub.result_label}'을(를) 기록했습니다.", "success")
    return _back(pid)


@router.post("/protocols/{pid}/comments", dependencies=[Depends(verify_csrf)])
async def comment(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    form = await request.form()
    try:
        wf.add_comment(db, user, p, str(form.get("body", "")), client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return _back(pid, "#comments")
    db.commit()
    return _back(pid, "#comments")


@router.post("/protocols/{pid}/ai-review", dependencies=[Depends(verify_csrf)])
def ai_review(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    wf.require_member(user, p)
    sub = p.current_submission
    pr = sub.latest_prereview if sub else None
    if p.is_editable or pr is None:
        result = run_ai_review(snapshot_protocol(p), wf.preview(db, p))
        if result is not None:
            request.session["ai_review"] = {"pid": p.id, "result": result}
    else:
        result = run_ai_review(sub.snapshot, pr.result)
        if result is not None:
            pr.ai_result = result
    if result is None:
        flash(request, "AI 기능이 비활성화되어 있습니다(IRB_AI_ENABLED).", "error")
        return _back(pid)
    audit.record(db, user, "prereview", "protocol", p.id, {"ai": True, "error": result.get("error")}, client_ip(request))
    db.commit()
    flash(request, result.get("error") or "AI 보조 검토가 완료되었습니다(참고용).", "error" if result.get("error") else "success")
    return _back(pid, "#prereview")


# --------------------------------------------------------------------- 출력 문서
@router.get("/protocols/{pid}/print/prereview")
def print_prereview(request: Request, pid: int, sub_id: int | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    sub = next((s for s in p.submissions if s.id == sub_id), p.current_submission) if sub_id else p.current_submission
    if sub and sub.latest_prereview:
        result, snapshot = sub.latest_prereview.result, sub.snapshot
    else:
        result, snapshot = wf.preview(db, p), None
    return render(request, "print/prereview.html", db, user, p=p, sub=sub, result=result, snapshot=snapshot)


# --------------------------------------------------------------------- 보완 답변
@router.post("/protocols/{pid}/revision-responses", dependencies=[Depends(verify_csrf)])
async def revision_responses(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    form = await request.form()
    responses = {int(k[5:]): str(v) for k, v in form.items() if k.startswith("resp_") and k[5:].isdigit()}
    try:
        n = wf.respond_revision_items(db, user, p, responses, client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return _back(pid)
    db.commit()
    flash(request, f"보완 답변 {n}건을 저장했습니다. 과제 내용·서류를 고친 뒤 재제출 패키지를 만드십시오.", "success")
    return RedirectResponse(f"/protocols/{pid}/revision", status_code=303)


# --------------------------------------------------------------------- 서류 자동 작성
@router.get("/protocols/{pid}/generate/{doc_type}")
def preview_generated(request: Request, pid: int, doc_type: str, fmt: str = "", source: str = "",
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    """미리보기: 기관 서식(DOCX/HWPX)이면 채운 파일을 내려받고, 기본 서식이면 화면에 표시한다.

    ``?fmt=pdf`` 는 PDF 변환기가 있을 때 PDF로, ``?source=builtin`` 은 기본 HTML 서식으로 본다.
    """
    p = _get(db, user, pid)
    if doc_type not in generatable_doc_types(db):
        raise HTTPException(404)
    from urllib.parse import quote

    from fastapi.responses import HTMLResponse, Response

    try:
        generated = build_document(db, p, doc_type, prefer_builtin=(source == "builtin" and doc_type in GENERATABLE))
    except wf.WorkflowError as e:
        raise HTTPException(400, str(e)) from e
    if generated.source == "builtin" and fmt != "pdf":
        return HTMLResponse(generated.content.decode("utf-8"))
    content, filename, media_type = generated.content, generated.filename, generated.media_type
    if fmt == "pdf":
        pdf = tpl_to_pdf(filename, content)
        if pdf is None:
            raise HTTPException(400, "PDF 변환을 사용할 수 없습니다(변환기 미설치). 원본 형식으로 내려받으십시오.")
        content, filename, media_type = pdf, filename.rsplit(".", 1)[0] + ".pdf", "application/pdf"
    audit.record(db, user, "template_fill", "protocol", p.id,
                 {"doc_type": doc_type, "template_id": generated.template_id, "preview": True, "fmt": fmt or generated.ext},
                 client_ip(request))
    db.commit()
    return Response(content, media_type=media_type,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})


@router.post("/protocols/{pid}/generate/{doc_type}", dependencies=[Depends(verify_csrf)])
def attach_generated(request: Request, pid: int, doc_type: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    if doc_type not in generatable_doc_types(db):
        raise HTTPException(404)
    if not (p.is_editable and wf.is_member(user, p)):
        flash(request, "작성중·보완요청 받음·변경 준비중 상태에서 연구팀만 서류를 첨부할 수 있습니다.", "error")
        return _back(pid, "#documents")
    source = generatable_doc_types(db)[doc_type]
    try:
        doc = generate_and_attach(db, user, p, doc_type, client_ip(request))
    except wf.WorkflowError as e:
        db.rollback()
        flash(request, str(e), "error")
        return _back(pid, "#documents")
    db.commit()
    how = "기관 서식으로 " if source == "institution" else ""
    flash(request, f"'{doc.filename}'을(를) {how}자동 작성하여 첨부했습니다. 내용을 확인하십시오.", "success")
    return _back(pid, "#documents")


# --------------------------------------------------------------------- 변경 대비표
@router.get("/submissions/{sid}/diff")
def submission_diff(request: Request, sid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from ..models import Submission

    sub = db.get(Submission, sid)
    if sub is None:
        raise HTTPException(404)
    p = _get(db, user, sub.protocol_id)
    prev = previous_snapshot(p, sub)
    changes = diff_snapshots(prev, sub.snapshot, get_ruleset()) if prev else []
    return render(request, "print/diff.html", db, user, p=p, sub=sub, changes=changes, has_prev=prev is not None,
                  items=wf.revision_items_for(db, next((s.id for s in reversed(p.submissions)
                                                       if s.id < sub.id and s.outcome == "revision_requested"), 0)))
