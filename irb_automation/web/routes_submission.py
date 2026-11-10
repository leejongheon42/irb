"""웹 기반 문서 제출 화면: 다중 업로드, 종류 추정, 미리보기, 버전 비교, 문서별 검토, 검토 대기열, 심의자료 합본."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session, object_session

from .. import audit
from ..database import get_db
from ..engine.ruleset import get_ruleset
from ..models import Document, Protocol, User
from ..submission import service
from ..submission.bundle import build_pdf, build_zip
from ..submission.compare import compare_documents
from ..submission.guess import guess_doc_type, guess_from_filename
from ..submission.preview import converted_pdf, document_text, preview_kind
from ..submission.versions import versions_of
from ..workflow import service as wf
from .deps import client_ip, current_user, flash, render, templates, verify_csrf

router = APIRouter()

FRAME_HEADERS = {"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "frame-ancestors 'self'"}
HTML_FRAME_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                  "frame-ancestors 'self'")


def _board_global(protocol: Protocol, user: User):
    """서류 탭 현황판(템플릿 전역 함수). 과제 객체가 속한 세션을 그대로 사용한다."""
    db = object_session(protocol)
    if db is None or user is None:
        return None
    return service.build_board(db, protocol, user)


templates.env.globals["submission_board"] = _board_global


def _protocol(db: Session, user: User, pid: int) -> Protocol:
    p = db.get(Protocol, pid)
    if p is None:
        raise HTTPException(404, "과제를 찾을 수 없습니다.")
    if not wf.can_view(db, user, p):
        raise HTTPException(403, "이 과제를 열람할 권한이 없습니다.")
    return p


def _doc(db: Session, user: User, doc_id: int) -> Document:
    doc = db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    _protocol(db, user, doc.protocol_id)
    return doc


def _disposition(kind: str, filename: str) -> str:
    return f"{kind}; filename*=UTF-8''{quote(filename)}"


# ------------------------------------------------------------------ 종류 추정 / 업로드
@router.get("/documents/guess")
def guess(name: str = "", user: User = Depends(current_user)):
    g = guess_from_filename(name)
    return JSONResponse({**g.to_dict(), "label": get_ruleset().label("document_types", g.doc_type)})


@router.post("/protocols/{pid}/documents/bulk", dependencies=[Depends(verify_csrf)])
async def bulk_upload(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    if not (wf.is_member(user, p) or user.is_admin):
        raise HTTPException(403, "서류 제출 권한이 없습니다.")
    form = await request.form()
    files = [f for f in form.getlist("files") if hasattr(f, "read") and getattr(f, "filename", "")]
    back = RedirectResponse(f"/protocols/{pid}#documents", status_code=303)
    if not files:
        flash(request, "제출할 파일을 선택하십시오.", "error")
        return back
    limit = int(service.policy()["max_files_per_upload"])
    if len(files) > limit:
        flash(request, f"한 번에 {limit}개까지 제출할 수 있습니다.", "error")
        return back
    allowed = set(get_ruleset().catalog("document_types"))
    details: list[tuple[str, str]] = []
    ok = warned = rejected = 0
    for i, up in enumerate(files):
        raw = await up.read()
        name = Path(up.filename).name
        chosen = str(form.get(f"doc_type_{i}") or form.get("doc_type_all") or "auto")
        if chosen not in allowed:
            g = guess_doc_type(name, raw, allowed=allowed)
            chosen = g.doc_type if g.confidence >= 0.5 else "other"
        try:
            doc, result = service.store_document(db, user, p, doc_type=chosen, filename=name, raw=raw,
                                                 ip=client_ip(request))
        except service.SubmissionError as e:
            rejected += 1
            details.append((f"거부: {name} — {e}", "error"))
            continue
        ok += 1
        label = service.doc_label(doc.doc_type)
        if result.warnings:
            warned += 1
            details.append((f"경고: {name} → {label} v{doc.version} — {' '.join(result.warnings)}", "info"))
        else:
            details.append((f"제출: {name} → {label} v{doc.version}", "success"))
    db.commit()
    category = "success" if ok and not rejected else ("error" if not ok else "info")
    flash(request, f"업로드 결과: 성공 {ok}건(경고 {warned}건) · 거부 {rejected}건", category)
    for msg, cat in details[:20]:
        flash(request, msg[:300], cat)
    return back


# ------------------------------------------------------------------ 검토 대기열
@router.get("/documents/{doc_id}/preview")
def preview(request: Request, doc_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _doc(db, user, doc_id)
    p = doc.protocol
    kind = preview_kind(doc)
    text = None
    if not doc.has_file:
        mode = "missing"
    elif kind in ("pdf", "image", "html"):
        mode = kind
    elif kind == "office":
        if converted_pdf(doc):
            mode = "pdf"
        else:
            text = document_text(doc)
            mode = "text" if text else "download"
    elif kind == "text":
        text = document_text(doc)
        mode = "text" if text is not None else "download"
    else:
        mode = "download"
    versions = versions_of(list(p.documents), doc.doc_type)
    audit.record(db, user, "document_preview", "protocol", p.id, {"doc_id": doc.id}, client_ip(request))
    db.commit()
    return render(
        request, "submission/preview.html", db, user,
        doc=doc, p=p, mode=mode, text=text, versions=versions,
        is_latest=bool(versions) and versions[0].id == doc.id,
        comments=service.comments_for(db, p, doc.doc_type),
        can_participate=service.can_participate(db, user, p),
        can_upload=service.can_upload(db, user, p, doc.doc_type)[0],
        validation=doc.validation or {},
    )


@router.get("/documents/{doc_id}/inline")
def inline(request: Request, doc_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _doc(db, user, doc_id)
    if not doc.has_file:
        raise HTTPException(404, "저장된 파일이 없습니다.")
    kind = preview_kind(doc)
    if kind == "pdf":
        return FileResponse(doc.stored_path, media_type="application/pdf",
                            headers={**FRAME_HEADERS, "Content-Disposition": _disposition("inline", doc.filename)})
    if kind == "office":
        data = converted_pdf(doc)
        if not data:
            raise HTTPException(415, "이 문서는 브라우저 미리보기용으로 변환할 수 없습니다. 다운로드하십시오.")
        return Response(data, media_type="application/pdf",
                        headers={**FRAME_HEADERS,
                                 "Content-Disposition": _disposition("inline", Path(doc.filename).stem + ".pdf")})
    if kind == "image":
        return FileResponse(doc.stored_path, media_type=doc.mime_type or "application/octet-stream",
                            headers={"Content-Disposition": _disposition("inline", doc.filename)})
    if kind == "html":
        return Response(Path(doc.stored_path).read_bytes(), media_type="text/html; charset=utf-8",
                        headers={"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": HTML_FRAME_CSP})
    if kind == "text":
        return Response(Path(doc.stored_path).read_bytes(), media_type="text/plain; charset=utf-8",
                        headers={**FRAME_HEADERS, "Content-Disposition": _disposition("inline", doc.filename)})
    raise HTTPException(415, "브라우저에서 미리볼 수 없는 형식입니다. 다운로드하십시오.")


# ------------------------------------------------------------------ 버전 비교
@router.get("/documents/{doc_id}/compare")
def compare(request: Request, doc_id: int, with_id: int | None = Query(None, alias="with"),
            user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _doc(db, user, doc_id)
    versions = versions_of(list(doc.protocol.documents), doc.doc_type)
    other = None
    if with_id is not None:
        other = db.get(Document, with_id)
        if other is None or other.protocol_id != doc.protocol_id or other.doc_type != doc.doc_type or other.id == doc.id:
            raise HTTPException(400, "같은 과제의 같은 서류 종류의 다른 버전만 비교할 수 있습니다.")
    else:
        older = [v for v in versions if (v.version, v.id) < (doc.version, doc.id)]
        other = older[0] if older else None
    if other is None:
        flash(request, "비교할 이전 버전이 없습니다.", "info")
        return RedirectResponse(f"/documents/{doc.id}/preview", status_code=303)
    old, new = sorted([doc, other], key=lambda d: (d.version, d.id))
    return render(request, "submission/compare.html", db, user, p=doc.protocol, old=old, new=new,
                  result=compare_documents(old, new), versions=versions, doc=doc)


# ------------------------------------------------------------------ 문서별 검토·의견
@router.post("/documents/{doc_id}/comments", dependencies=[Depends(verify_csrf)])
async def comment(request: Request, doc_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    doc = _doc(db, user, doc_id)
    form = await request.form()
    try:
        service.add_comment(db, user, doc, str(form.get("body", "")), client_ip(request))
    except wf.PermissionDenied as e:
        raise HTTPException(403, str(e)) from e
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/documents/{doc.id}/preview#comments", status_code=303)
    db.commit()
    return RedirectResponse(f"/documents/{doc.id}/preview#comments", status_code=303)


# ------------------------------------------------------------------ 심의자료 합본
def _bundle_protocol(db: Session, user: User, pid: int) -> Protocol:
    p = _protocol(db, user, pid)
    if not service.can_participate(db, user, p):
        raise HTTPException(403, "합본은 연구팀·IRB 사무국·배정 심의위원만 내려받을 수 있습니다.")
    return p


@router.get("/protocols/{pid}/bundle.zip")
def bundle_zip(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _bundle_protocol(db, user, pid)
    data = build_zip(p, user)
    audit.record(db, user, "document_bundle", "protocol", p.id, {"format": "zip", "size": len(data)}, client_ip(request))
    db.commit()
    name = f"{p.protocol_no or f'protocol-{p.id}'}_심의자료.zip"
    return Response(data, media_type="application/zip", headers={"Content-Disposition": _disposition("attachment", name)})


@router.get("/protocols/{pid}/bundle.pdf")
def bundle_pdf(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _bundle_protocol(db, user, pid)
    data, rows = build_pdf(p, user)
    audit.record(db, user, "document_bundle", "protocol", p.id,
                 {"format": "pdf", "merged": sum(1 for r in rows if r["page"]), "separate": sum(1 for r in rows if not r["page"])},
                 client_ip(request))
    db.commit()
    name = f"{p.protocol_no or f'protocol-{p.id}'}_심의자료_합본.pdf"
    return Response(data, media_type="application/pdf", headers={"Content-Disposition": _disposition("attachment", name)})
