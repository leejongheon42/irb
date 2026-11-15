"""Management of public IRB sources and project preparation."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..models import Protocol, Role, Submission, SubmissionKind, User
from ..public_irb import checklists, sources
from ..public_irb.checklist_models import ChecklistDecision, ChecklistDefinition, ChecklistResponse
from ..public_irb.records import RequirementState
from ..public_irb.source_models import SourceDocument
from ..workflow import service as wf
from .deps import current_user, render, require_roles, verify_csrf

router = APIRouter()
admin = require_roles(Role.ADMIN)


def _project(db: Session, user: User, pid: int) -> Protocol:
    protocol = db.get(Protocol, pid)
    if protocol is None or protocol.committee != "public_irb" or not wf.can_view(db, user, protocol):
        raise HTTPException(404)
    return protocol


@router.get("/admin/public-irb")
def registry(request: Request, draft_source: int | None = None, user: User = Depends(admin), db: Session = Depends(get_db)):
    rows = db.scalars(select(SourceDocument).order_by(SourceDocument.series, SourceDocument.number)).all()
    selected = db.get(SourceDocument, draft_source) if draft_source is not None else None
    draft = ""
    if selected and selected.owner in ("researcher", "committee"):
        try:
            items = checklists.draft_items(selected)
            if items:
                draft = ChecklistDefinition(source_document_id=selected.id, owner=selected.owner, items=items).model_dump_json(indent=2)
        except (ValidationError, sources.SourceError) as exc:
            raise HTTPException(422, "원문에서 점검표 초안을 읽을 수 없습니다.") from exc
    return render(request, "public_irb/registry.html", db, user, sources=rows, draft=draft)


@router.post("/admin/public-irb/seed", dependencies=[Depends(verify_csrf)])
def seed(user: User = Depends(admin), db: Session = Depends(get_db)):
    sources.seed_catalog(db)
    db.commit()
    return RedirectResponse("/admin/public-irb", status_code=303)


@router.post("/admin/public-irb/source", dependencies=[Depends(verify_csrf)])
async def upload_source(request: Request, user: User = Depends(admin), db: Session = Depends(get_db)):
    form = await request.form()
    upload = form.get("file")
    if not hasattr(upload, "read"):
        raise HTTPException(422, "원문 파일을 선택하십시오.")
    data = await upload.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413)
    try:
        key = sources.SourceKey.model_validate({key: form.get(key) for key in ("series", "number", "version")})
        original = sources.get_source(db, key)
        registration = sources.SourceRegistration.model_validate({
            "key": key, "title": form.get("title") or (original.title if original else ""),
            "origin": form.get("origin") or (original.origin if original else ""),
            "owner": original.owner if original else form.get("owner", "researcher"),
            "original_filename": upload.filename,
            "applies_to": original.applies_to if original else str(form.get("applies_to", "")).split(),
            "notion_block_id": str(form.get("notion_block_id", "")) or (original.notion_block_id if original else ""),
            "reference_date": original.reference_date if original else None,
        })
        row = sources.register_source(db, registration, data)
    except (ValidationError, sources.SourceError) as exc:
        raise HTTPException(422, "원문 식별 정보와 판본을 확인하십시오.") from exc
    audit.record(db, user, "public_irb_source_upload", "source_document", row.id, {"sha256": row.sha256})
    db.commit()
    return RedirectResponse("/admin/public-irb", status_code=303)


@router.post("/admin/public-irb/source/{sid}/verify", dependencies=[Depends(verify_csrf)])
async def verify_source(sid: int, request: Request, user: User = Depends(admin), db: Session = Depends(get_db)):
    row = db.get(SourceDocument, sid)
    if row is None:
        raise HTTPException(404)
    form = await request.form()
    try:
        sources.verify_source(db, row, sources.SourceVerification(verified_by=str(user.id), manual_note=str(form.get("note", ""))))
        sources.activate_source(db, row)
    except sources.SourceError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit.record(db, user, "public_irb_source_activate", "source_document", sid, {"sha256": row.sha256})
    db.commit()
    return RedirectResponse("/admin/public-irb", status_code=303)


@router.get("/public-irb/sources/{sid}/download")
def download(sid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(SourceDocument, sid)
    if row is None or (not user.is_admin and (not row.verified or row.owner != "researcher")):
        raise HTTPException(404)
    try:
        raw = sources.source_bytes(row)
    except sources.SourceError as exc:
        raise HTTPException(404) from exc
    return Response(raw, media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(row.original_filename)}"})


@router.post("/admin/public-irb/checklists", dependencies=[Depends(verify_csrf)])
async def register_checklist(request: Request, user: User = Depends(admin), db: Session = Depends(get_db)):
    form = await request.form()
    try:
        definition = ChecklistDefinition.model_validate_json(str(form.get("definition", "")))
        checklists.register_form(db, user, definition)
    except (ValidationError, wf.WorkflowError) as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return RedirectResponse("/admin/public-irb", status_code=303)


@router.get("/protocols/{pid}/public-irb")
def project(request: Request, pid: int, submission_id: int | None = None, kind: str | None = None,
            user: User = Depends(current_user), db: Session = Depends(get_db)):
    protocol = _project(db, user, pid)
    submission = _submission(db, protocol, submission_id)
    kind = submission.kind.value if submission else _kind(protocol, kind)
    responses = db.scalars(select(ChecklistResponse).where(
        ChecklistResponse.protocol_id == protocol.id, ChecklistResponse.scope == (submission_id or 0))).all()
    applicable = checklists.forms_for(db, protocol, submission)
    return render(request, "public_irb/project.html", db, user, p=protocol, kind=kind,
                  requirements=(submission.latest_prereview.result["checklist"] if submission and submission.latest_prereview
                                else wf.preview(db, protocol, kind)["checklist"]),
                  submission_id=submission_id,
                  reviewers=db.scalars(select(User).where(User.role.in_((Role.ADMIN, Role.REVIEWER)), User.is_active.is_(True))).all()
                  if user.is_admin else [], can_edit=user.role != Role.REVIEWER and wf.is_member(user, protocol),
                  forms=[(form, checklists.rows(protocol, form, [r for r in responses if r.form_id == form.id], submission),
                          checklists.can_decide(user, protocol, form), checklists.verified_for(form, submission))
                         for form in applicable],
                  answers=checklists.ANSWER_LABELS, directions=checklists.DIRECTION_LABELS)


@router.post("/protocols/{pid}/public-irb/{operation}", dependencies=[Depends(verify_csrf)])
async def update_project(request: Request, pid: int, operation: str, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    protocol = _project(db, user, pid)
    form = await request.form()
    try:
        if operation == "reviewers":
            wf.assign_reviewers(db, user, protocol, [int(v) for v in form.getlist("reviewer_id")])
        elif operation == "answer":
            raw_submission = str(form.get("submission_id", ""))
            decision = ChecklistDecision.model_validate({"protocol_id": pid, **{
                key: form.get(key) for key in ("form_id", "item_key", "answer", "evidence_ref")
            }, "submission_id": raw_submission or None})
            submission = _submission(db, protocol, decision.submission_id)
            checklists.save_answer(db, user, decision)
            db.commit()
            suffix = f"?submission_id={submission.id}" if submission else ""
            return RedirectResponse(f"/protocols/{pid}/public-irb{suffix}", status_code=303)
        elif operation == "requirement":
            wf.require_member(user, protocol)
            kind = _kind(protocol, str(form.get("kind", "")))
            doc_type, status = str(form.get("doc_type", "")), str(form.get("status", ""))
            item = next((item for item in wf.preview(db, protocol, kind)["checklist"] if item["doc"] == doc_type), None)
            if (item is None or item["channel"] not in ("external_eirb", "eirb_or_file")
                    or status not in ("prepared", "declared_submitted", "not_applicable")):
                raise HTTPException(422)
            db.merge(RequirementState(protocol_id=pid, kind=kind, doc_type=doc_type, status=status, declared_by=user.id))
            audit.record(db, user, "public_irb_requirement", "protocol", pid, {"doc": doc_type, "status": status})
        else:
            raise HTTPException(404)
    except wf.PermissionDenied as exc:
        raise HTTPException(403, str(exc)) from exc
    except (ValueError, wf.WorkflowError) as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    suffix = f"?kind={kind}" if operation == "requirement" else ""
    return RedirectResponse(f"/protocols/{pid}/public-irb{suffix}", status_code=303)


def _kind(protocol: Protocol, kind: str | None) -> str:
    selected = kind or wf.preview_kind(protocol)
    try:
        return SubmissionKind(selected).value
    except ValueError as exc:
        raise HTTPException(422, "유효하지 않은 제출 유형입니다.") from exc


def _submission(db: Session, protocol: Protocol, sid: int | None) -> Submission | None:
    if sid is None:
        return None
    row = db.get(Submission, sid)
    if row is None or row.protocol_id != protocol.id:
        raise HTTPException(404)
    return row
