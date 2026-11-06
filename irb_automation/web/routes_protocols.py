"""연구과제: 작성·제출·심의·결정·보고 화면."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, esign
from ..config import get_settings
from ..database import get_db
from ..doctemplates.service import to_pdf as tpl_to_pdf
from ..documents import GENERATABLE, build_document, generatable_doc_types, generate_and_attach
from ..engine.ai_review import run_ai_review
from ..engine.facts import ProtocolFacts
from ..engine.ruleset import get_ruleset
from ..models import (
    Agreement,
    Decision,
    DecisionType,
    DestructionRecord,
    Document,
    OutputRequest,
    Protocol,
    ProtocolStatus,
    Recommendation,
    ReviewAssignment,
    ReviewType,
    Role,
    SubmissionKind,
    User,
)
from ..submission import service as submission_service
from ..submission.guess import guess_doc_type
from ..submission.stamp import stamp_after_decision
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
    if user.is_irb_staff:  # 사무국 목록에는 타인의 작성중 과제를 노출하지 않음
        items = [p for p in items if p.status != S.DRAFT or user.id in p.member_ids()]
    if status:
        items = [p for p in items if p.status.value == status]
    if q:
        ql = q.lower()
        items = [p for p in items if ql in p.title.lower() or ql in (p.protocol_no or "").lower() or ql in p.pi.full_name.lower()]
    return render(request, "protocols/list.html", db, user, items=items, status=status, q=q)


@router.get("/protocols/new")
def new_protocol_form(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role not in (Role.RESEARCHER, Role.ADMIN, Role.COORDINATOR):
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
    flash(request, "과제가 생성되었습니다. 서류를 첨부하고 자동 사전점검 후 제출하십시오.", "success")
    return _back(p.id)


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
    my_assignment = None
    if sub:
        my_assignment = next((a for a in sub.assignments if a.reviewer_id == user.id), None)
    co_names = [db.get(User, uid) for uid in p.co_investigators or []]
    logs = []
    if user.is_irb_staff or wf.is_member(user, p):
        from ..models import AuditLog

        logs = db.scalars(
            select(AuditLog).where(AuditLog.entity_type == "protocol", AuditLog.entity_id == p.id).order_by(AuditLog.id.desc())
        ).all()
    diff = None
    if sub and sub.kind.value in ("resubmission", "amendment"):
        prev = previous_snapshot(p, sub)
        diff = diff_snapshots(prev, sub.snapshot, get_ruleset()) if prev else None
    revision_items = wf.open_revision_items(db, p) if p.status == S.REVISION_REQUESTED else []
    last_revision_items = []
    if sub and sub.kind.value == "resubmission":
        prev_sub = next((s for s in reversed(p.submissions) if s.id != sub.id and s.outcome == "revision_requested"), None)
        last_revision_items = wf.revision_items_for(db, prev_sub.id) if prev_sub else []
    signatures = []
    for s_ in p.submissions:
        signatures += esign.signatures_for(db, "submission", s_.id)
        if s_.decision:
            signatures += esign.signatures_for(db, "decision", s_.decision.id)
    related, similar = _knowledge_context(db, user, p, preview, sub)
    return render(
        request, "protocols/detail.html", db, user,
        p=p, sub=sub, preview=preview, my_assignment=my_assignment, related=related, similar=similar,
        diff=diff, revision_items=revision_items, last_revision_items=last_revision_items,
        signatures=signatures, generatable=generatable_doc_types(db),
        agreements=[a for a in db.scalars(select(Agreement).where(Agreement.protocol_id == p.id))],
        outputs=db.scalars(select(OutputRequest).where(OutputRequest.protocol_id == p.id).order_by(OutputRequest.id.desc())).all(),
        destruction=db.scalars(select(DestructionRecord).where(DestructionRecord.protocol_id == p.id)).first(),
        co_names=[u for u in co_names if u], reviewers=_users(db, Role.REVIEWER, Role.CHAIR),
        is_member=wf.is_member(user, p), logs=logs,
        DecisionType=DecisionType, ReviewType=ReviewType, Recommendation=Recommendation,
        allowed_decisions=sorted(wf.ALLOWED_DECISIONS[sub.kind], key=lambda d: list(DecisionType).index(d)) if sub else [],
        ai_enabled=get_settings().ai_review_enabled,
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
    if not (wf.is_member(user, p) or user.is_irb_staff):
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
    p = _get(db, user, pid)
    form = await request.form()
    if form.get("attest") != "on":
        flash(request, "연구자 서약에 동의해야 제출할 수 있습니다.", "error")
        return _back(pid, "#precheck")
    try:
        sub = wf.submit(db, user, p, client_ip(request))
        esign.sign(db, user, str(form.get("password", "")), "연구책임자 제출 서약", "submission", sub.id,
                   sub.snapshot, client_ip(request))
    except (wf.WorkflowError, esign.SignatureError) as e:
        db.rollback()
        flash(request, str(e), "error")
        return _back(pid, "#precheck")
    db.commit()
    flash(request, f"{sub.kind.label} 신청이 접수되었습니다 (과제번호 {p.protocol_no}, 자동분류: {sub.review_type.label}).", "success")
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
        sub = wf.submit_report(db, user, p, k, report, client_ip(request))
    except wf.WorkflowError as e:
        db.rollback()
        flash(request, str(e), "error")
        return _back(pid)
    db.commit()
    flash(request, f"{sub.kind.label}이(가) 제출되었습니다.", "success")
    return _back(pid)


# --------------------------------------------------------------------- 사무국/위원
@router.post("/protocols/{pid}/revision", dependencies=[Depends(verify_csrf)])
async def request_revision(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    form = await request.form()
    items: list[tuple[str, str]] = []
    sub = p.current_submission
    findings = {f["code"]: f for f in (sub.latest_prereview.result["findings"] if sub and sub.latest_prereview else [])}
    for code in form.getlist("finding"):
        f = findings.get(str(code))
        if f:
            items.append((f["code"], f["message"] + (f" → {f['recommendation']}" if f["recommendation"] else "")))
    for line in str(form.get("custom_items", "")).splitlines():
        if line.strip():
            items.append(("", line.strip()))
    try:
        wf.request_revision(db, user, p, str(form.get("message", "")), client_ip(request), items=items)
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return _back(pid)
    db.commit()
    flash(request, "연구자에게 보완을 요청했습니다.", "success")
    return _back(pid)


@router.post("/protocols/{pid}/assign", dependencies=[Depends(verify_csrf)])
async def assign(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    form = await request.form()
    ids = [int(v) for v in form.getlist("reviewers") if str(v).isdigit()]
    primary = form.get("primary")
    rtype = form.get("review_type")
    try:
        wf.assign_reviewers(
            db, user, p, ids,
            int(primary) if primary and str(primary).isdigit() else (ids[0] if ids else None),
            ReviewType(rtype) if rtype else None,
            parse_date(str(form.get("due_date", ""))),
            client_ip(request),
        )
    except wf.WorkflowError as e:
        db.rollback()
        flash(request, str(e), "error")
        return _back(pid, "#review")
    db.commit()
    flash(request, "심의위원을 배정했습니다.", "success")
    return _back(pid, "#review")


@router.post("/assignments/{aid}/review", dependencies=[Depends(verify_csrf)])
async def review(request: Request, aid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    a = db.get(ReviewAssignment, aid)
    if a is None:
        raise HTTPException(404)
    form = await request.form()
    checklist = {k[3:]: form.get(k) for k in form if k.startswith("ck_")}
    pid = a.submission.protocol_id
    try:
        rec = form.get("recommendation") or "approve"
        wf.submit_review(
            db, user, a, Recommendation(rec), str(form.get("comments", "")),
            checklist, form.get("coi") == "on", client_ip(request),
        )
    except (wf.WorkflowError, ValueError) as e:
        flash(request, str(e), "error")
        return _back(pid, "#review")
    db.commit()
    flash(request, "심의의견을 제출했습니다.", "success")
    return _back(pid, "#review")


@router.post("/protocols/{pid}/decide", dependencies=[Depends(verify_csrf)])
async def decide(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _get(db, user, pid)
    form = await request.form()
    try:
        d = wf.decide(
            db, user, p, DecisionType(str(form.get("decision"))),
            str(form.get("conditions", "")), str(form.get("comments", "")),
            parse_date(str(form.get("meeting_date", ""))), client_ip(request),
        )
        esign.record_signature(db, user, "심의결과 확정", "decision", d.id, decision_payload(d), client_ip(request))
    except (wf.WorkflowError, ValueError) as e:
        db.rollback()
        flash(request, str(e), "error")
        return _back(pid, "#review")
    stamped = stamp_after_decision(db, user, p, d, client_ip(request))
    db.commit()
    flash(request, f"심의결과 '{d.decision.label}' 처리되었습니다. 통지서 번호 {d.letter_no}"
                   + (f" · 승인 도장본 {len(stamped)}건 생성" if stamped else ""), "success")
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
    wf.require_staff(user)
    sub = p.current_submission
    pr = sub.latest_prereview if sub else None
    if pr is None:
        flash(request, "자동 사전검토 결과가 없습니다.", "error")
        return _back(pid)
    result = run_ai_review(sub.snapshot, pr.result)
    if result is None:
        flash(request, "AI 사전검토 보조 기능이 비활성화되어 있습니다(IRB_AI_REVIEW_ENABLED).", "error")
        return _back(pid)
    pr.ai_result = result
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


@router.get("/decisions/{did}/letter")
def letter(request: Request, did: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = db.get(Decision, did)
    if d is None:
        raise HTTPException(404)
    p = _get(db, user, d.submission.protocol_id)
    audit.record(db, user, "letter_view", "protocol", p.id, {"letter_no": d.letter_no}, client_ip(request))
    db.commit()
    co = [db.get(User, uid) for uid in p.co_investigators or []]
    sigs = esign.signatures_for(db, "submission", d.submission_id) + esign.signatures_for(db, "decision", d.id)
    return render(request, "print/letter.html", db, user, p=p, d=d, sub=d.submission, co=[u for u in co if u], sigs=sigs)


def decision_payload(d: Decision) -> dict:
    return {
        "letter_no": d.letter_no, "decision": d.decision.value, "review_type": d.review_type.value,
        "conditions": d.conditions, "comments": d.comments,
        "approval_start": str(d.approval_start), "approval_end": str(d.approval_end),
        "snapshot": d.submission.snapshot,
    }


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
    flash(request, f"보완 답변 {n}건을 저장했습니다. 내용을 수정한 뒤 재제출하십시오.", "success")
    return _back(pid)


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
    if not (p.is_editable or user.is_irb_staff):
        flash(request, "작성중·보완요청·변경작성 상태에서만 서류를 첨부할 수 있습니다.", "error")
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
