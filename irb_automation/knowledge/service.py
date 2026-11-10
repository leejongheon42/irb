"""관련 정보 검색 서비스(다른 구성 요소와의 계약 인터페이스).

    search(db, query, k=5, kinds=None, user=None) -> list[dict]
    related_for_findings(db, findings, k=3) -> dict[str, list[dict]]
    similar_protocols(db, protocol, k=5, user=None) -> list[dict]
    answer(db, question, k=6, user=None) -> dict

노출 정책
- 정책 지식(법령·지침·SOP·FAQ·검토 의견 가이드·등록 문서): 모든 로그인 사용자
- 선례 지식(과거 과제·심의 결정): 규칙 세트 knowledge.precedent_roles(기본: 관리자)는
  전체, 그 외 사용자(연구자·데이터관리자)는 자신이 열람할 수 있는 과제(workflow.can_view)의 것만.
  타 과제는 존재 여부도 노출하지 않는다.
- user=None 호출(내부 파이프라인)은 필터 없이 검색한다. 연구자에게 보여줄 결과라면 user를 넘겨야 한다.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..deid import patterns as P
from ..engine.ruleset import get_ruleset
from ..models import Protocol, Role, User
from . import index as ix
from .models import KIND_LABELS, POLICY_KINDS, PRECEDENT_KINDS, KnowledgeChunk, KnowledgeQuery, KnowledgeSource
from .sources import builtin_loaded, index_stamp, reindex_protocols, seed_knowledge

log = logging.getLogger(__name__)

DEFAULT_PRECEDENT_ROLES = ("admin",)


def _policy() -> dict[str, Any]:
    return {
        "bm25_k1": 1.2,
        "bm25_b": 0.75,
        "title_boost": 2,
        "snippet_chars": 220,
        "answer_max_chunks": 6,
        "precedent_roles": list(DEFAULT_PRECEDENT_ROLES),
        "ai_answer_max_tokens": 4000,
        **get_ruleset().data.get("knowledge", {}),
    }


# ------------------------------------------------------------------ 색인 준비
def _engine_key(db: Session) -> str:
    return str(db.get_bind().url)


def ensure_ready(db: Session) -> None:
    """첫 사용 시 내장 지식을 적재하고, 과제·결정 변경분을 선례 지식에 반영한다."""
    if not builtin_loaded(db):
        seed_knowledge(db)
    reindex_protocols(db)


def get_index(db: Session) -> ix.BM25Index:
    ensure_ready(db)
    pol = _policy()

    def build() -> ix.BM25Index:
        index = ix.BM25Index(k1=float(pol["bm25_k1"]), b=float(pol["bm25_b"]))
        rows = db.execute(
            select(KnowledgeChunk, KnowledgeSource).join(KnowledgeSource).where(KnowledgeSource.active.is_(True))
        ).all()
        for chunk, src in rows:
            doc = ix.IndexedChunk(
                chunk_id=chunk.id, source_id=src.id, kind=src.kind, title=src.title, ref=src.ref,
                url=src.url or f"/knowledge/source/{src.id}", heading=chunk.heading, text=chunk.text,
                protocol_id=(src.meta or {}).get("protocol_id"),
            )
            index.add(doc, ix.chunk_tokens(src.title, src.ref, chunk.heading, chunk.text, int(pol["title_boost"])))
        index.finalize()
        return index

    return ix.cached_index(_engine_key(db), index_stamp(db), build)


# ------------------------------------------------------------------ 권한
def precedent_full_access(user: User | None) -> bool:
    if user is None:
        return True
    roles = set(_policy().get("precedent_roles") or DEFAULT_PRECEDENT_ROLES)
    return user.role.value in roles or user.role == Role.ADMIN


def viewable_protocol_ids(db: Session, user: User) -> set[int]:
    from ..workflow.service import can_view

    return {p.id for p in db.scalars(select(Protocol).where(Protocol.protocol_no.is_not(None))) if can_view(db, user, p)}


def _allow_fn(db: Session, user: User | None):
    if precedent_full_access(user):
        return None
    allowed = viewable_protocol_ids(db, user)  # type: ignore[arg-type]

    def allow(doc: ix.IndexedChunk) -> bool:
        if doc.kind in PRECEDENT_KINDS:
            return doc.protocol_id in allowed
        return True

    return allow


# ------------------------------------------------------------------ 검색
def _hit(score: float, doc: ix.IndexedChunk, q_tokens: set[str], terms: list[str], snippet_chars: int) -> dict[str, Any]:
    snippet = ix.best_sentences(doc.text, q_tokens, snippet_chars)
    return {
        "source_id": doc.source_id,
        "chunk_id": doc.chunk_id,
        "kind": doc.kind,
        "kind_label": KIND_LABELS.get(doc.kind, doc.kind),
        "title": doc.title,
        "ref": doc.ref,
        "heading": doc.heading,
        "snippet": snippet,
        "highlight": ix.highlight(snippet, terms),
        "score": round(score, 3),
        "url": doc.url,
        "view_url": doc.url if doc.kind in PRECEDENT_KINDS else f"/knowledge/source/{doc.source_id}",
        "external_url": doc.url if doc.url.startswith("http") else "",
    }


def search(
    db: Session,
    query: str,
    k: int = 5,
    kinds: list[str] | None = None,
    user: User | None = None,
    *,
    exclude_source_ids: set[int] | None = None,
    one_per_source: bool = True,
) -> list[dict[str, Any]]:
    """BM25 검색. 같은 출처에서는 가장 관련 높은 청크 하나만(one_per_source) 반환한다."""
    query = (query or "").strip()
    if not query:
        return []
    index = get_index(db)
    pol = _policy()
    q_tokens = ix.tokenize(query)
    terms = ix.query_terms(query)
    allow = _allow_fn(db, user)
    raw = index.search(q_tokens, kinds=set(kinds) if kinds else None, allow=allow, limit=max(k * 8, 40))
    hits: list[dict[str, Any]] = []
    seen: set[int] = set()
    qset = set(q_tokens)
    for score, doc in raw:
        if exclude_source_ids and doc.source_id in exclude_source_ids:
            continue
        if one_per_source and doc.source_id in seen:
            continue
        seen.add(doc.source_id)
        hits.append(_hit(score, doc, qset, terms, int(pol["snippet_chars"])))
        if len(hits) >= k:
            break
    return hits


def record_query(db: Session, user: User | None, question: str, mode: str, result_count: int, ip: str = "") -> KnowledgeQuery:
    q = KnowledgeQuery(user_id=user.id if user else None, question=mask_pii(question)[0][:2000], mode=mode,
                       result_count=result_count)
    db.add(q)
    db.flush()
    audit.record(db, user, "knowledge_search" if mode == "search" else "knowledge_answer", "knowledge", q.id,
                 {"mode": mode, "results": result_count}, ip)
    return q


# ------------------------------------------------------------------ 검토 의견 → 근거
def related_for_findings(db: Session, findings: list[dict[str, Any]], k: int = 3) -> dict[str, list[dict[str, Any]]]:
    """검토 의견(code) → 관련 근거(해당 코드의 검토 의견 가이드 우선 + 법령·지침·SOP 검색)."""
    if not findings:
        return {}
    get_index(db)
    guides: dict[str, KnowledgeSource] = {}
    for src in db.scalars(select(KnowledgeSource).where(KnowledgeSource.kind == "finding_guide",
                                                       KnowledgeSource.active.is_(True))):
        code = (src.meta or {}).get("code")
        if code:
            guides[code] = src
    out: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        code = str(f.get("code") or "")
        if not code or code in out:
            continue
        hits: list[dict[str, Any]] = []
        guide = guides.get(code) or (guides.get("SENSITIVE") if code.startswith("SENSITIVE_") else None)
        if guide and guide.chunks:
            first = guide.chunks[0]
            hits.append({
                "source_id": guide.id, "chunk_id": first.id, "kind": guide.kind,
                "kind_label": KIND_LABELS[guide.kind], "title": guide.title, "ref": guide.ref,
                "heading": first.heading, "snippet": ix.best_sentences(first.text, set(), 220),
                "highlight": ix.highlight(ix.best_sentences(first.text, set(), 220), []),
                "score": 999.0, "url": f"/knowledge/source/{guide.id}",
                "view_url": f"/knowledge/source/{guide.id}", "external_url": "",
            })
        query = " ".join(str(f.get(x) or "") for x in ("legal", "message", "recommendation"))
        more = search(db, query, k=k + 2, kinds=["law", "guideline", "sop", "uploaded"],
                      exclude_source_ids={h["source_id"] for h in hits})
        hits.extend(more)
        out[code] = hits[:k]
    return out


# ------------------------------------------------------------------ 유사 과제
def similar_protocols(db: Session, protocol: Protocol, k: int = 5, user: User | None = None) -> list[dict[str, Any]]:
    """연구 내용이 비슷한 과거 과제(선례). 연구자에게 보여줄 때는 user를 넘겨 권한 필터를 적용한다."""
    from .sources import protocol_text

    get_index(db)
    _, text = protocol_text(protocol)
    own = db.scalars(select(KnowledgeSource.id).where(KnowledgeSource.key == f"protocol:{protocol.id}")).all()
    hits = search(db, text, k=k, kinds=["protocol"], user=user, exclude_source_ids=set(own))
    out = []
    for h in hits:
        src = db.get(KnowledgeSource, h["source_id"])
        pid = (src.meta or {}).get("protocol_id") if src else None
        p = db.get(Protocol, pid) if pid else None
        if p is None or p.id == protocol.id:
            continue
        last = next((s for s in reversed(p.submissions) if s.result), None)
        out.append({
            "protocol_id": p.id,
            "protocol_no": p.protocol_no,
            "title": p.title,
            "status": p.status.label,
            "decision": last.result_label if last else "",
            "review_type": p.review_type.label if p.review_type else "",
            "score": h["score"],
            "url": f"/protocols/{p.id}",
        })
    return out


# ------------------------------------------------------------------ 개인정보 마스킹
_MASK_KINDS = {"rrn", "card", "mobile", "landline", "email", "passport", "ip"}


def mask_pii(text: str) -> tuple[str, list[str]]:
    """질문의 개인정보(주민번호·연락처·이메일·여권·상세주소·'OOO 환자' 형태 이름)를 마스킹."""
    found: list[str] = []
    out = text or ""
    for kind, pat, repl in P.TEXT_SCRUB_PATTERNS:
        if kind in _MASK_KINDS or (kind == "address" and pat is P.ADDRESS_DETAIL):
            out, n = pat.subn(repl, out)
            if n:
                found.append(kind)
    try:
        from ..deid.transforms import _HONORIFIC_NAME, _NAME_STOPWORDS
    except ImportError:  # pragma: no cover
        return out, found

    def _name(m: re.Match[str]) -> str:
        if m[1] not in _NAME_STOPWORDS and P.looks_like_korean_name(m[1]):
            found.append("name")
            return "[이름]" + m[2] + m[3]
        return m[0]

    out = _HONORIFIC_NAME.sub(_name, out)
    return out, sorted(set(found))


# ------------------------------------------------------------------ 질의응답
SYSTEM_PROMPT = """당신은 병원 기관생명윤리위원회(IRB) 사무국의 규정 안내 보조자입니다.
제공된 문서(법령·지침 요약, 기관 SOP, 검토 의견 가이드, FAQ, 심의 선례)만 근거로 한국어로 답하십시오.

- 문서에 근거가 없으면 추측하지 말고 "제공된 자료에서 근거를 찾지 못했습니다"라고 답하고 IRB 사무국 문의를 안내하십시오.
- 법령 요약 문서는 원문이 아니므로, 결론에 영향을 주는 경우 원문 확인이 필요하다고 덧붙이십시오.
- 심의 결과(승인·면제 여부)를 단정하지 말고, 판단은 IRB가 한다는 점을 유지하십시오.
- 3~6문장 내외로 간결하게, 근거 문장을 인용하며 답하십시오."""


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _document_block(client: Any, text: str, title: str) -> dict[str, Any]:
    maker = getattr(client, "text_document_block", None)
    if maker is not None:
        return maker(text, title, citations=True)
    return {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": text},
            "title": title, "citations": {"enabled": True}}


def _chunk_text(db: Session, hit: dict[str, Any]) -> str:
    chunk = db.get(KnowledgeChunk, hit["chunk_id"])
    return chunk.text if chunk else hit["snippet"]


def _ai_answer(db: Session, question: str, hits: list[dict[str, Any]]) -> dict[str, Any]:
    from ..ai import client  # 선택 구성 요소 - 없으면 ImportError

    if not client.ai_enabled():
        raise RuntimeError("AI 기능이 비활성화되어 있습니다.")
    docs = []
    for h in hits:
        title = f"{h['title']}" + (f" — {h['ref']}" if h["ref"] else "") + (f" · {h['heading']}" if h["heading"] else "")
        docs.append(_document_block(client, _chunk_text(db, h), title[:300]))
    response = client.create_message(
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": [*docs, {"type": "text", "text": f"질문: {question}"}]}],
        max_tokens=int(_policy()["ai_answer_max_tokens"]),
        effort="medium",
        purpose="knowledge_answer",
    )
    parts: list[str] = []
    citations: list[dict[str, Any]] = []
    index_of: dict[tuple[int, str], int] = {}
    for block in _get(response, "content", []) or []:
        if _get(block, "type") != "text":
            continue
        text = _get(block, "text", "") or ""
        marks = []
        for c in _get(block, "citations", None) or []:
            di = _get(c, "document_index")
            if di is None or not (0 <= int(di) < len(hits)):
                continue
            h = hits[int(di)]
            cited = (_get(c, "cited_text", "") or "").strip()
            keyv = (int(di), cited)
            if keyv not in index_of:
                index_of[keyv] = len(citations) + 1
                citations.append({"title": h["title"], "ref": h["ref"], "cited_text": cited, "source_id": h["source_id"],
                                  "chunk_id": h["chunk_id"], "url": h["url"], "kind": h["kind"], "n": index_of[keyv]})
            marks.append(index_of[keyv])
        parts.append(text + "".join(f" [{n}]" for n in sorted(set(marks))))
    answer_text = "".join(parts).strip()
    if not answer_text:
        raise RuntimeError("AI 응답이 비어 있습니다.")
    return {"answer": answer_text, "citations": citations, "mode": "ai"}


def _extractive_answer(question: str, hits: list[dict[str, Any]], db: Session) -> dict[str, Any]:
    if not hits:
        return {"answer": "제공된 자료에서 관련 근거를 찾지 못했습니다. 질문을 바꾸어 보거나 IRB 사무국에 문의하십시오.",
                "citations": [], "mode": "extractive"}
    q_tokens = set(ix.tokenize(question))
    # 최상위 근거 + (있으면) 법령·지침 근거 + 다음 순위 근거로 3개를 고른다
    chosen = [hits[0]]
    official = next((h for h in hits[1:] if h["kind"] in ("law", "guideline", "sop") and h not in chosen), None)
    if official and chosen[0]["kind"] not in ("law", "guideline", "sop"):
        chosen.append(official)
    for h in hits[1:]:
        if len(chosen) >= 3:
            break
        if h not in chosen:
            chosen.append(h)
    lines = []
    citations = []
    for i, h in enumerate(chosen, start=1):
        sent = ix.best_sentences(_chunk_text(db, h), q_tokens, 260)
        lines.append(f"{i}. {sent} [{i}]")
        citations.append({"title": h["title"], "ref": h["ref"], "cited_text": sent, "source_id": h["source_id"],
                          "chunk_id": h["chunk_id"], "url": h["url"], "kind": h["kind"], "n": i})
    text = "관련 근거에서 찾은 핵심 내용입니다(자동 발췌).\n" + "\n".join(lines)
    if any(h["kind"] == "law" for h in chosen):
        text += "\n※ 법령 요약은 원문이 아니므로 최종 판단 전 원문을 확인하십시오."
    return {"answer": text, "citations": citations, "mode": "extractive"}


def answer(db: Session, question: str, k: int = 6, user: User | None = None, *, ip: str = "",
           record: bool = True) -> dict[str, Any]:
    """근거 인용 질의응답. AI가 켜져 있으면 Claude Citations로, 아니면 추출형으로 답한다.

    질문의 개인정보는 검색·AI 전송·기록 전에 마스킹한다. user=None(내부 호출)은 정책 지식만 사용한다.
    """
    question = (question or "").strip()
    if not question:
        return {"answer": "질문을 입력하십시오.", "citations": [], "mode": "extractive", "warnings": [], "hits": []}
    masked, pii = mask_pii(question)
    warnings: list[str] = []
    if pii:
        warnings.append("질문에 포함된 개인정보(" + ", ".join(pii) + ")를 마스킹한 뒤 처리했습니다. 개인정보는 입력하지 마십시오.")
    kinds = None if user is not None else list(POLICY_KINDS)
    k = min(k, int(_policy()["answer_max_chunks"]))
    hits = search(db, masked, k=k, kinds=kinds, user=user)
    result: dict[str, Any] | None = None
    if hits:
        try:
            result = _ai_answer(db, masked, hits)
        except ImportError:
            result = None
        except Exception as e:  # AIError, AIUnavailable, 비활성 등 → 추출형으로 대체
            if "비활성" not in str(e):
                warnings.append(f"AI 답변을 생성하지 못해 자동 발췌로 대체했습니다({type(e).__name__}).")
            log.info("knowledge AI answer fallback: %s", e)
            result = None
    if result is None:
        result = _extractive_answer(masked, hits, db)
    result["warnings"] = warnings
    result["pii_masked"] = bool(pii)
    result["question"] = masked
    result["hits"] = hits
    if record:
        record_query(db, user, masked, result["mode"], len(hits), ip)
    return result
