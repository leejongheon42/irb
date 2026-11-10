"""관련 정보 검색 화면: 지식 검색, 근거 인용 질의응답, 지식 문서 관리."""
from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..knowledge import service as ks
from ..knowledge import sources as kn_sources
from ..knowledge.models import KIND_LABELS, PRECEDENT_KINDS, UPLOAD_KINDS, KnowledgeChunk, KnowledgeSource
from ..models import Role, User
from .deps import client_ip, current_user, flash, render, require_roles, verify_csrf

router = APIRouter()
staff_only = require_roles(Role.ADMIN)

EXAMPLE_QUERIES = [
    "가명정보 동의 없이 연구",
    "제16조제3항 동의 면제",
    "주민등록번호 수집",
    "자유기술 경과기록 스크러빙",
    "타 기관 자료 결합",
    "결과 반출 소규모 셀",
]
EXAMPLE_QUESTIONS = [
    "의무기록 후향 연구는 심의면제가 되나요?",
    "동의 면제를 받으려면 무엇을 소명해야 하나요?",
    "건강보험 자료와 결합하려면 어떤 절차가 필요한가요?",
    "가명정보 처리 기록에는 무엇을 남겨야 하나요?",
]


def _kb_stats(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(KnowledgeSource.kind, func.count(KnowledgeSource.id))
        .where(KnowledgeSource.active.is_(True)).group_by(KnowledgeSource.kind)
    ).all()
    return {k: n for k, n in rows}


@router.get("/knowledge")
def knowledge_search(request: Request, q: str = "", kind: str = "", user: User = Depends(current_user),
                     db: Session = Depends(get_db)):
    hits: list[dict] = []
    facets: Counter[str] = Counter()
    kind = kind if kind in KIND_LABELS else ""
    if q.strip():
        hits = ks.search(db, q, k=20, kinds=[kind] if kind else None, user=user)
        broad = ks.search(db, q, k=60, user=user) if kind else hits
        facets = Counter(h["kind"] for h in broad)
        ks.record_query(db, user, q, "search", len(hits), client_ip(request))
    else:
        ks.ensure_ready(db)
    db.commit()
    precedent_full = ks.precedent_full_access(user)
    stats = _kb_stats(db)
    if not precedent_full:  # 타 과제 존재 여부(건수)도 노출하지 않는다
        stats = {k: v for k, v in stats.items() if k not in PRECEDENT_KINDS}
    return render(request, "knowledge/search.html", db, user, q=q, kind=kind, hits=hits, facets=facets,
                  kind_labels=KIND_LABELS, stats=stats, examples=EXAMPLE_QUERIES,
                  precedent_full=precedent_full)


@router.get("/knowledge/ask")
def knowledge_ask_form(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return render(request, "knowledge/ask.html", db, user, result=None, question="", examples=EXAMPLE_QUESTIONS)


@router.post("/knowledge/ask", dependencies=[Depends(verify_csrf)])
async def knowledge_ask(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()
    question = str(form.get("question", "")).strip()[:1000]
    result = ks.answer(db, question, user=user, ip=client_ip(request)) if question else None
    db.commit()
    if result is None:
        flash(request, "질문을 입력하십시오.", "error")
    return render(request, "knowledge/ask.html", db, user, result=result,
                  question=result["question"] if result else "", examples=EXAMPLE_QUESTIONS)


@router.get("/knowledge/source/{sid}")
def knowledge_source(request: Request, sid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    src = db.get(KnowledgeSource, sid)
    if src is None or (not src.active and not user.is_admin):
        raise HTTPException(404, "지식 문서를 찾을 수 없습니다.")
    if src.kind in PRECEDENT_KINDS and not ks.precedent_full_access(user):
        pid = (src.meta or {}).get("protocol_id")
        if pid not in ks.viewable_protocol_ids(db, user):
            raise HTTPException(404, "지식 문서를 찾을 수 없습니다.")
    return render(request, "knowledge/source.html", db, user, src=src, kind_labels=KIND_LABELS)


# ------------------------------------------------------------------ 관리(IRB 사무국)
@router.get("/admin/knowledge")
def knowledge_admin(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    ks.ensure_ready(db)
    db.commit()
    counts = dict(db.execute(select(KnowledgeChunk.source_id, func.count(KnowledgeChunk.id))
                             .group_by(KnowledgeChunk.source_id)).all())
    sources = db.scalars(select(KnowledgeSource).where(KnowledgeSource.kind.not_in(PRECEDENT_KINDS))
                         .order_by(KnowledgeSource.kind, KnowledgeSource.title)).all()
    precedent = db.execute(select(KnowledgeSource.kind, func.count(KnowledgeSource.id))
                           .where(KnowledgeSource.kind.in_(PRECEDENT_KINDS)).group_by(KnowledgeSource.kind)).all()
    return render(request, "knowledge/admin.html", db, user, sources=sources, counts=counts,
                  precedent=dict(precedent), kind_labels=KIND_LABELS, upload_kinds=UPLOAD_KINDS)


@router.post("/admin/knowledge", dependencies=[Depends(verify_csrf)])
async def knowledge_upload(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    form = await request.form()
    upload = form.get("file")
    raw, filename = None, ""
    if hasattr(upload, "read") and getattr(upload, "filename", ""):
        raw = await upload.read()
        filename = upload.filename
    try:
        src = kn_sources.add_uploaded_source(
            db, user,
            title=str(form.get("title", "")), kind=str(form.get("kind", "sop")),
            ref=str(form.get("ref", "")), url=str(form.get("url", "")), version=str(form.get("version", "")),
            filename=filename, raw=raw or None, text=str(form.get("text", "")), ip=client_ip(request),
        )
    except kn_sources.KnowledgeError as e:
        db.rollback()
        flash(request, str(e), "error")
        return RedirectResponse("/admin/knowledge", status_code=303)
    db.commit()
    warn = (src.meta or {}).get("warnings") or []
    flash(request, f"'{src.title}'을(를) {len(src.chunks)}개 구간으로 색인했습니다." + (" " + " ".join(warn) if warn else ""),
          "success")
    return RedirectResponse("/admin/knowledge", status_code=303)


def _source_or_404(db: Session, sid: int) -> KnowledgeSource:
    src = db.get(KnowledgeSource, sid)
    if src is None:
        raise HTTPException(404)
    return src


@router.post("/admin/knowledge/{sid}/toggle", dependencies=[Depends(verify_csrf)])
def knowledge_toggle(request: Request, sid: int, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    src = _source_or_404(db, sid)
    kn_sources.set_active(db, src, not src.active, user, client_ip(request))
    db.commit()
    flash(request, f"'{src.title}' {'활성화' if src.active else '비활성화'}", "success")
    return RedirectResponse("/admin/knowledge", status_code=303)


@router.post("/admin/knowledge/{sid}/reindex", dependencies=[Depends(verify_csrf)])
def knowledge_reindex(request: Request, sid: int, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    src = _source_or_404(db, sid)
    try:
        n = kn_sources.reindex_source(db, src, user, client_ip(request))
    except kn_sources.KnowledgeError as e:
        db.rollback()
        flash(request, str(e), "error")
        return RedirectResponse("/admin/knowledge", status_code=303)
    db.commit()
    flash(request, f"'{src.title}' 재색인 완료({n}개 구간)", "success")
    return RedirectResponse("/admin/knowledge", status_code=303)


@router.post("/admin/knowledge/{sid}/delete", dependencies=[Depends(verify_csrf)])
def knowledge_delete(request: Request, sid: int, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    src = _source_or_404(db, sid)
    try:
        kn_sources.delete_source(db, src, user, client_ip(request))
    except kn_sources.KnowledgeError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/admin/knowledge", status_code=303)
    db.commit()
    flash(request, "지식 문서를 삭제했습니다.", "success")
    return RedirectResponse("/admin/knowledge", status_code=303)


@router.post("/admin/knowledge/reindex-precedents", dependencies=[Depends(verify_csrf)])
def knowledge_reindex_precedents(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    stats = kn_sources.reindex_protocols(db, force=True)
    from .. import audit

    audit.record(db, user, "knowledge_reindex", "knowledge", None, {"precedents": stats}, client_ip(request))
    db.commit()
    flash(request, f"선례 재색인 완료: 과제 {stats['protocols']}건, 결정 {stats['decisions']}건", "success")
    return RedirectResponse("/admin/knowledge", status_code=303)


@router.post("/admin/knowledge/reseed", dependencies=[Depends(verify_csrf)])
def knowledge_reseed(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    stats = kn_sources.seed_knowledge(db, force=True)
    from .. import audit

    audit.record(db, user, "knowledge_reindex", "knowledge", None, {"builtin": stats}, client_ip(request))
    db.commit()
    flash(request, f"내장 지식 갱신: 신규 {stats['created']} · 갱신 {stats['updated']}", "success")
    return RedirectResponse("/admin/knowledge", status_code=303)
