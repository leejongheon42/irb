"""Store a user's uploads and review them with the QC engine after commit."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, event, select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from ..ai import client as ai
from ..config import get_settings
from ..database import SessionLocal
from ..engine.ruleset import get_ruleset
from ..models import User
from ..qc import category_scores
from ..qc import service as qc
from ..qc.models import QCRun
from ..submission.guess import guess_doc_type
from ..submission.validate import ALLOWED_EXT, validate_upload
from ..timeutil import now
from .models import MvpUpload

log = logging.getLogger(__name__)
ALLOWED_UPLOADS = ALLOWED_EXT - {".zip"}
UNREADABLE = "본문을 읽지 못함"
QUEUED = "심사 대기 중"
ACTIVE = ("queued", "running")
STALE_AFTER = timedelta(minutes=10)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="irb-mvp")
# 이 프로세스에서 지금 돌고 있는 심사. AI 호출은 한 단계에서 10분 넘게 걸릴 수 있어 시간만으로 중단을 판정하지 않는다.
_running: set[int] = set()
_running_lock = threading.Lock()


class ReviewUnavailable(Exception):
    """There are no reviewable files or the requested retry is unavailable."""


class SupersededReview(Exception):
    """The worker no longer owns this attempt."""


def pending(user_id: int) -> tuple[ColumnElement[bool], ...]:
    return (MvpUpload.user_id == user_id, MvpUpload.run_id.is_(None))


def save_upload(db: Session, user_id: int, name: str, raw: bytes, size: int, paths: list[Path]) -> MvpUpload:
    settings = get_settings()
    result = validate_upload(name, raw, max_bytes=settings.max_upload_mb * 1024 * 1024,
                             allowed_ext=ALLOWED_UPLOADS, size=size)
    row = MvpUpload(user_id=user_id, name=(result.filename or "이름 없는 파일")[:255], size=result.size,
                    fmt=result.ext.removeprefix(".")[:12], status="ok" if result.ok else "rejected")
    messages = result.warnings if result.ok else result.errors
    if result.ext == ".zip":
        messages.append("ZIP 파일을 풀어 서류를 개별로 올려 주세요.")
    if result.ok:
        doc = qc._read_doc("", result.filename, raw, "other")
        guess = guess_doc_type(result.filename, extracted_text=doc.text)
        row.doc_type = guess.doc_type if guess.confidence >= 0.5 else "other"
        row.doc_type_label = get_ruleset().label("document_types", row.doc_type)
        if not doc.text.strip():
            messages.append("심사 중 AI OCR로 본문 인식 시도" if doc.fmt in ("pdf", "png", "jpeg") and ai.ai_enabled()
                            else UNREADABLE)
        folder = settings.data_dir / "mvp" / str(user_id)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{uuid4().hex}_{result.safe_name}"
        paths.append(path)
        path.write_bytes(raw)
        row.path = str(path)
    row.message = " · ".join(messages)
    db.add(row)
    return row


def remove_pending(db: Session, user_id: int, file_id: int | None = None) -> bool:
    filters = pending(user_id)
    if file_id is not None:
        filters += (MvpUpload.id == file_id,)
    paths = db.scalars(delete(MvpUpload).where(*filters).returning(MvpUpload.path)).all()
    db.commit()
    for path in filter(None, paths):
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            log.exception("Failed to remove a stored MVP file")
    return bool(paths)


def _dispatch(db: Session, run_id: int, attempt: str) -> None:
    if get_settings().pipeline_sync:
        db.commit()
        _review(run_id, attempt)
        db.expire_all()
        return

    def after_commit(_session: Session) -> None:
        _executor.submit(_review, run_id, attempt)

    event.listen(db, "after_commit", after_commit, once=True)
    db.commit()


def start_review(db: Session, user: User) -> int:
    attempt = uuid4().hex
    run = QCRun(created_by=user.id, status="queued", stage=QUEUED, mvp_attempt=attempt, stage_updated_at=now())
    db.add(run)
    db.flush()
    # The write claims the files atomically against removal or another start request.
    names = db.scalars(update(MvpUpload).where(*pending(user.id), MvpUpload.status == "ok")
                       .values(run_id=run.id).returning(MvpUpload.name)).all()
    if not names:
        db.rollback()
        raise ReviewUnavailable("심사할 수 있는 서류를 먼저 올려 주세요.")
    run.title = ", ".join(names)[:200]
    run_id = run.id
    _dispatch(db, run_id, attempt)
    return run_id


def retry_review(db: Session, run_id: int, user_id: int) -> None:
    attempt = uuid4().hex
    claimed = db.scalar(update(QCRun).where(QCRun.id == run_id, QCRun.created_by == user_id,
                       QCRun.mvp_attempt.is_not(None), QCRun.status == "failed")
                       .values(mvp_attempt=attempt, status="queued", stage=QUEUED, error="", stage_updated_at=now())
                       .returning(QCRun.id))
    if claimed is None:
        raise ReviewUnavailable("실패한 심사만 다시 실행할 수 있습니다.")
    _dispatch(db, run_id, attempt)


def expire_stalled(db: Session, user_id: int, run_id: int | None = None) -> None:
    stale = update(QCRun).where(QCRun.created_by == user_id, QCRun.mvp_attempt.is_not(None),
                               QCRun.status.in_(ACTIVE), QCRun.stage_updated_at < now() - STALE_AFTER)
    if run_id is not None:
        stale = stale.where(QCRun.id == run_id)
    with _running_lock:
        alive = tuple(_running)
    if alive:
        stale = stale.where(QCRun.id.not_in(alive))
    db.execute(stale.values(status="failed", stage="", error="심사가 중단되었습니다. 다시 심사해 주세요."))
    db.commit()


def _advance(db: Session, run_id: int, attempt: str, previous: str, status: str, stage: str) -> None:
    with db.no_autoflush:
        changed = db.scalar(update(QCRun).where(QCRun.id == run_id, QCRun.mvp_attempt == attempt,
                            QCRun.status == previous).values(status=status, stage=stage, stage_updated_at=now())
                            .returning(QCRun.id))
    if changed is None:
        raise SupersededReview
    db.commit()


def _review(run_id: int, attempt: str) -> None:
    with _running_lock:
        _running.add(run_id)
    try:
        _run_review(run_id, attempt)
    finally:
        with _running_lock:
            _running.discard(run_id)


def _run_review(run_id: int, attempt: str) -> None:
    try:
        with SessionLocal() as db:
            _advance(db, run_id, attempt, "queued", "running", "서류 읽는 중")
            run = db.scalars(select(QCRun).where(QCRun.id == run_id)).one()
            user = db.scalars(select(User).where(User.id == run.created_by)).one()
            files = db.scalars(select(MvpUpload).where(MvpUpload.run_id == run_id).order_by(MvpUpload.id)).all()
            use_ai = ai.ai_enabled()
            docs = []
            store = {}
            for index, file in enumerate(files, start=1):
                key = f"d{index}"
                store[key] = file.path
                doc = qc._read_doc(key, file.name, Path(file.path).read_bytes(), file.doc_type, allow_ai_ocr=use_ai)
                if use_ai and file.doc_type == "other" and doc.text.strip():
                    guess = guess_doc_type(file.name, extracted_text=doc.text)
                    if guess.confidence >= 0.5:
                        doc.doc_type = guess.doc_type
                docs.append(doc)

            def stage(label: str) -> None:
                _advance(db, run_id, attempt, "running", "running", label)

            qc.run(db, user, docs, qr=run, use_ai=True, store=store, on_stage=stage, score_run=category_scores.score_run)
            _advance(db, run_id, attempt, "running", "done", "채점 완료")
    except SupersededReview:
        log.info("MVP review %s no longer owns its attempt", run_id)
    except Exception:  # Background job boundary: failures must reach the status endpoint.
        log.exception("MVP review %s failed", run_id)
        with SessionLocal() as db:
            db.execute(update(QCRun).where(QCRun.id == run_id, QCRun.mvp_attempt == attempt, QCRun.status.in_(ACTIVE))
                       .values(status="failed", stage="", error="서류 심사 중 오류가 발생했습니다. 다시 심사해 주세요."))
            db.commit()
