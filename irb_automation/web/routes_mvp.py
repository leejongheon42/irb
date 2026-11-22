"""Upload, review and scorecard pages for the MVP."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..engine.ruleset import get_ruleset
from ..models import User
from ..mvp import service
from ..mvp.evidence import evidence_image, findings_evidence
from ..mvp.models import MvpUpload
from ..qc.category_scores import SEVERITIES, VERDICTS
from ..qc.models import QCRun
from ..timeutil import now
from .deps import current_user, flash, render, verify_csrf

router = APIRouter()
log = logging.getLogger(__name__)
CurrentUser = Annotated[User, Depends(current_user)]
Db = Annotated[Session, Depends(get_db)]


class UploadRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    fmt: str
    size: int
    doc_type_label: str
    status: str
    message: str


class ReviewStatus(BaseModel):
    status: str
    stage: str
    score: int | None
    verdict: str | None


def _review(db: Session, user: User, run_id: int) -> QCRun:
    run = db.get(QCRun, run_id)
    if run is None or run.created_by != user.id or run.mvp_attempt is None:
        raise HTTPException(404)
    if run.status in service.ACTIVE and run.stage_updated_at and now() - run.stage_updated_at > service.STALE_AFTER:
        service.expire_stalled(db, user.id, run_id)
        db.refresh(run)
    return run


@router.get("/mvp")
def upload_page(request: Request, user: CurrentUser, db: Db) -> HTMLResponse:
    service.expire_stalled(db, user.id)
    files = db.scalars(select(MvpUpload).where(*service.pending(user.id)).order_by(MvpUpload.id)).all()
    history = db.scalars(select(QCRun).where(QCRun.created_by == user.id, QCRun.mvp_attempt.is_not(None))
                         .order_by(QCRun.id.desc()).limit(10)).all()
    return render(request, "mvp/upload.html", db, user, files=files, history=history,
                  can_review=any(file.status == "ok" for file in files),
                  allowed=", ".join(ext[1:].upper() for ext in sorted(service.ALLOWED_UPLOADS)),
                  max_mb=get_settings().max_upload_mb, verdicts=VERDICTS)


@router.post("/mvp/files", dependencies=[Depends(verify_csrf)], response_model=None)
def add_files(request: Request, user: CurrentUser, db: Db,
              files: Annotated[list[UploadFile] | None, File()] = None) -> list[UploadRow] | RedirectResponse:
    limit = get_settings().max_upload_mb * 1024 * 1024
    paths: list[Path] = []
    rows = []
    committed = False
    try:
        for file in files or []:
            raw = file.file.read(limit + 1)
            rows.append(service.save_upload(db, user.id, file.filename or "", raw, file.size or len(raw), paths))
        db.commit()
        committed = True
    finally:
        if not committed:
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.exception("Failed to clean up an uncommitted MVP upload")
    if "application/json" in request.headers.get("accept", ""):
        return [UploadRow.model_validate(row) for row in rows]
    return RedirectResponse("/mvp", status_code=303)


@router.post("/mvp/files/{file_id}/delete", dependencies=[Depends(verify_csrf)])
def delete_file(file_id: int, user: CurrentUser, db: Db) -> RedirectResponse:
    if not service.remove_pending(db, user.id, file_id):
        raise HTTPException(404)
    return RedirectResponse("/mvp", status_code=303)


@router.post("/mvp/files/clear", dependencies=[Depends(verify_csrf)])
def clear_files(user: CurrentUser, db: Db) -> RedirectResponse:
    service.remove_pending(db, user.id)
    return RedirectResponse("/mvp", status_code=303)


@router.post("/mvp/review", dependencies=[Depends(verify_csrf)])
def start_review(request: Request, user: CurrentUser, db: Db) -> RedirectResponse:
    try:
        run_id = service.start_review(db, user)
    except service.ReviewUnavailable as error:
        flash(request, str(error), "error")
        return RedirectResponse("/mvp", status_code=303)
    return RedirectResponse(f"/mvp/review/{run_id}", status_code=303)


@router.post("/mvp/review/{run_id}/retry", dependencies=[Depends(verify_csrf)])
def retry_review(request: Request, run_id: int, user: CurrentUser, db: Db) -> RedirectResponse:
    _review(db, user, run_id)
    try:
        service.retry_review(db, run_id, user.id)
    except service.ReviewUnavailable as error:
        flash(request, str(error), "error")
    return RedirectResponse(f"/mvp/review/{run_id}", status_code=303)


@router.get("/mvp/review/{run_id}")
def review_page(request: Request, run_id: int, user: CurrentUser, db: Db) -> HTMLResponse:
    run = _review(db, user, run_id)
    files = db.scalars(select(MvpUpload).where(MvpUpload.run_id == run_id).order_by(MvpUpload.id)).all()
    return render(request, "mvp/review.html", db, user, run=run, files=files, verdicts=VERDICTS)


@router.get("/api/mvp/review/{run_id}")
def review_status(run_id: int, user: CurrentUser, db: Db) -> ReviewStatus:
    run = _review(db, user, run_id)
    done = run.status == "done"
    return ReviewStatus(status=run.status, stage=run.stage, score=run.score if done and not run.counts["na"] else None,
                        verdict=run.verdict if done else None)


@router.get("/mvp/review/{run_id}/scores", response_model=None)
def score_page(request: Request, run_id: int, user: CurrentUser, db: Db,
               tab: Literal["scores", "criteria"] = "scores") -> HTMLResponse | RedirectResponse:
    run = _review(db, user, run_id)
    if run.status != "done":
        return RedirectResponse(f"/mvp/review/{run_id}", status_code=303)
    locations = run.counts.get("source_locations")
    if tab == "scores" and locations is None:
        locations = findings_evidence(run)
    return render(request, "mvp/scores.html", db, user, run=run, tab=tab, verdicts=VERDICTS,
                  severities=SEVERITIES, issues={issue["id"]: issue for issue in run.issues},
                  source_locations=locations or {}, ruleset=get_ruleset())


@router.get("/mvp/review/{run_id}/evidence/{issue_id}/{index}.png")
def source_image(run_id: int, issue_id: int, index: int, user: CurrentUser, db: Db) -> Response:
    run = _review(db, user, run_id)
    image = evidence_image(run, issue_id, index) if run.status == "done" else None
    if image is None:
        raise HTTPException(404)
    return Response(image, media_type="image/png", headers={"Cache-Control": "private, no-store"})
