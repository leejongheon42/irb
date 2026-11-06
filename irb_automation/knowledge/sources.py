"""지식 출처 관리: 내장 지식 적재, 문서 청크 분할, 업로드 문서, 과제·심의 선례 자동 색인."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..engine.ruleset import get_ruleset
from ..models import Decision, Protocol, Submission, User
from ..timeutil import now
from .models import UPLOAD_KINDS, KnowledgeChunk, KnowledgeSource

BUILTIN_DIR = Path(__file__).parent / "builtin"
TEXT_EXT = {".txt", ".md", ".markdown", ".csv"}
EXTRACT_EXT = {".pdf", ".docx", ".hwpx", ".hwp"}
MAX_CHUNK = 900
MIN_CHUNK = 60


class KnowledgeError(Exception):
    pass


# ================================================================== 청크 분할
_HEADING = re.compile(
    r"^\s*(?:"
    r"#{1,6}\s+.+"  # 마크다운 제목
    r"|제\s*\d+\s*(?:장|절|관)\b.*"  # 장·절
    r"|제\s*\d+\s*조(?:의\s*\d+)?\s*(?:\(.*?\))?.*"  # 조문
    r"|\d{1,2}(?:\.\d{1,2}){0,2}\.?\s+\S.{0,80}"  # 1. / 1.2 / 1.2.3 제목
    r"|[가-하]\.\s+\S.{0,80}"  # 가. 나.
    r"|【.+】|\[.+\]"  # 【제목】 [제목]
    r")\s*$"
)


_ARTICLE_HEAD = re.compile(r"^(제\s*\d+\s*조(?:의\s*\d+)?\s*(?:\([^)]*\))?)\s*(.*)$")


def _is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 100:
        return False
    if s.endswith(("다.", "니다.", "함.", "음.")) and not s.startswith(("#", "제")):
        return False
    return bool(_HEADING.match(s))


def split_into_chunks(text: str, default_heading: str = "") -> list[tuple[str, str]]:
    """제목(마크다운·조문·번호) 경계로 나누고, 긴 구간은 문단 단위로 다시 나눈다. [(heading, text)]"""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    sections: list[tuple[str, list[str]]] = []
    heading = default_heading
    buf: list[str] = []
    for line in text.split("\n"):
        if _is_heading(line):
            if any(x.strip() for x in buf):
                sections.append((heading, buf))
            heading = line.strip().lstrip("#").strip()
            buf = []
            # '제1조(목적) 본문...'처럼 조문 제목 뒤에 본문이 이어지면 나눠서 본문을 보존
            m = _ARTICLE_HEAD.match(heading)
            if m and m.group(2).strip():
                heading, buf = m.group(1).strip(), [m.group(2).strip()]
        else:
            buf.append(line)
    if any(x.strip() for x in buf):
        sections.append((heading, buf))

    chunks: list[tuple[str, str]] = []
    for head, lines in sections:
        body = "\n".join(lines).strip()
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        cur = ""
        for p in paras:
            if len(p) > MAX_CHUNK:
                # 아주 긴 문단은 문장 경계로 자른다
                pieces = []
                for sent in re.split(r"(?<=[.!?다])\s+", p):
                    pieces.extend(sent[i : i + MAX_CHUNK] for i in range(0, len(sent), MAX_CHUNK))
                for piece in pieces:
                    if len(cur) + len(piece) + 1 > MAX_CHUNK and cur:
                        chunks.append((head, cur.strip()))
                        cur = ""
                    cur += piece + " "
                continue
            if len(cur) + len(p) + 2 > MAX_CHUNK and cur:
                chunks.append((head, cur.strip()))
                cur = ""
            cur += p + "\n\n"
        if cur.strip():
            chunks.append((head, cur.strip()))

    # 너무 짧은 청크는 앞 청크와 합친다(같은 제목일 때)
    merged: list[tuple[str, str]] = []
    for head, body in chunks:
        if merged and len(body) < MIN_CHUNK and merged[-1][0] == head:
            merged[-1] = (head, merged[-1][1] + "\n" + body)
        else:
            merged.append((head, body))
    return merged


def _set_chunks(db: Session, source: KnowledgeSource, chunks: list[tuple[str, str]]) -> None:
    source.chunks.clear()
    db.flush()
    for seq, (heading, body) in enumerate(chunks):
        source.chunks.append(KnowledgeChunk(seq=seq, heading=heading[:300], text=body, length=len(body)))
    source.updated_at = now()
    db.flush()


# ================================================================== 내장 지식
def builtin_documents() -> list[dict[str, Any]]:
    docs = []
    for path in sorted(BUILTIN_DIR.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        version = str(data.get("version", "1"))
        for src in data.get("sources", []):
            docs.append({**src, "version": f"{version}:{src.get('version', '1')}"})
    return docs


def seed_knowledge(db: Session, force: bool = False) -> dict[str, int]:
    """내장 지식(법령·지침 요약, 검토 의견 가이드, FAQ)을 적재. 멱등이며 버전이 바뀐 출처만 갱신한다(force=전체)."""
    stats = {"created": 0, "updated": 0, "unchanged": 0}
    for doc in builtin_documents():
        key = f"builtin:{doc['key']}"
        src = db.scalars(select(KnowledgeSource).where(KnowledgeSource.key == key)).first()
        body_sections = [(s.get("heading", ""), s.get("text", "").strip()) for s in doc.get("sections", [])]
        if src and src.version == doc["version"] and not force:
            stats["unchanged"] += 1
            continue
        if src is None:
            src = KnowledgeSource(key=key, kind=doc["kind"], title=doc["title"])
            db.add(src)
            stats["created"] += 1
        else:
            stats["updated"] += 1
        src.kind = doc["kind"]
        src.title = doc["title"]
        src.ref = doc.get("ref", "")
        src.url = doc.get("url", "")
        src.version = doc["version"]
        src.meta = {"code": doc.get("code", ""), "builtin": True, "note": doc.get("note", "")}
        db.flush()
        chunks: list[tuple[str, str]] = []
        for heading, text in body_sections:
            for h, t in split_into_chunks(text, heading or doc["title"]):
                chunks.append((h or heading, t))
        _set_chunks(db, src, chunks)
    return stats


def builtin_loaded(db: Session) -> bool:
    return bool(db.scalar(select(func.count(KnowledgeSource.id)).where(KnowledgeSource.key.like("builtin:%"))))


# ================================================================== 업로드 문서
def extract_upload_text(filename: str, raw: bytes) -> tuple[str, list[str]]:
    """TXT/MD는 직접, PDF·DOCX·HWPX·HWP는 문서 인식 모듈(docproc)이 있을 때만 추출."""
    ext = Path(filename).suffix.lower()
    if ext in TEXT_EXT:
        from ..deid.pipeline import decode_bytes

        try:
            text, _ = decode_bytes(raw)
        except ValueError as e:
            raise KnowledgeError(str(e)) from e
        return text, []
    if ext in EXTRACT_EXT:
        try:
            from ..docproc.text import extract_text  # 문서 인식 모듈(별도 구성 요소)
        except ImportError as e:
            raise KnowledgeError(
                "PDF·DOCX·HWP 텍스트 추출 모듈(docproc)이 설치되어 있지 않습니다. TXT/MD로 업로드하거나 내용을 붙여넣으십시오."
            ) from e
        result = extract_text(filename, raw)
        text = getattr(result, "text", "") or ""
        if not text.strip():
            raise KnowledgeError("문서에서 텍스트를 추출하지 못했습니다(스캔 이미지일 수 있습니다).")
        return text, list(getattr(result, "warnings", []) or [])
    raise KnowledgeError(f"지원하지 않는 형식입니다({ext or '확장자 없음'}). TXT·MD·PDF·DOCX·HWPX·HWP를 사용하십시오.")


def add_uploaded_source(
    db: Session,
    user: User | None,
    *,
    title: str,
    kind: str = "sop",
    ref: str = "",
    url: str = "",
    version: str = "",
    filename: str = "",
    raw: bytes | None = None,
    text: str = "",
    ip: str = "",
) -> KnowledgeSource:
    if kind not in UPLOAD_KINDS:
        raise KnowledgeError("지원하지 않는 지식 유형입니다.")
    if not title.strip():
        raise KnowledgeError("제목을 입력하십시오.")
    warnings: list[str] = []
    if raw:
        settings = get_settings()
        if len(raw) > settings.max_upload_mb * 1024 * 1024:
            raise KnowledgeError("파일 크기 제한을 초과했습니다.")
        text, warnings = extract_upload_text(filename, raw)
    elif text.strip():
        filename = filename or "pasted.txt"
        raw = text.encode("utf-8")
    else:
        raise KnowledgeError("파일을 선택하거나 내용을 입력하십시오.")
    chunks = split_into_chunks(text, title.strip())
    if not chunks:
        raise KnowledgeError("색인할 내용이 없습니다.")
    folder = get_settings().data_dir / "knowledge"
    folder.mkdir(parents=True, exist_ok=True)
    ext = Path(filename).suffix.lower() or ".txt"
    path = folder / f"{uuid.uuid4().hex}{ext}"
    path.write_bytes(raw)
    src = KnowledgeSource(
        kind=kind, title=title.strip()[:300], ref=ref.strip()[:300], url=url.strip()[:500],
        version=version.strip()[:60], file_path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
        meta={"filename": Path(filename).name, "warnings": warnings}, created_by=user.id if user else None,
    )
    db.add(src)
    db.flush()
    _set_chunks(db, src, chunks)
    audit.record(db, user, "knowledge_upload", "knowledge", src.id,
                 {"title": src.title, "kind": kind, "chunks": len(chunks), "sha256": src.sha256}, ip)
    return src


def reindex_source(db: Session, src: KnowledgeSource, user: User | None = None, ip: str = "") -> int:
    if src.is_builtin:
        src.version = ""  # 다음 seed에서 다시 적재
        seed_knowledge(db)
        db.refresh(src)
    elif src.is_auto:
        reindex_protocols(db, force=True)
        db.refresh(src)
    else:
        if not src.file_path or not Path(src.file_path).exists():
            raise KnowledgeError("원본 파일이 없어 재색인할 수 없습니다.")
        raw = Path(src.file_path).read_bytes()
        text, _ = extract_upload_text(src.meta.get("filename") or src.file_path, raw)
        _set_chunks(db, src, split_into_chunks(text, src.title))
    audit.record(db, user, "knowledge_reindex", "knowledge", src.id, {"chunks": len(src.chunks)}, ip)
    return len(src.chunks)


def set_active(db: Session, src: KnowledgeSource, active: bool, user: User | None = None, ip: str = "") -> None:
    src.active = active
    src.updated_at = now()
    db.flush()
    audit.record(db, user, "knowledge_update", "knowledge", src.id, {"active": active}, ip)


def delete_source(db: Session, src: KnowledgeSource, user: User | None = None, ip: str = "") -> None:
    if src.is_builtin or src.is_auto:
        raise KnowledgeError("내장·자동 색인 지식은 삭제할 수 없습니다. 비활성화하십시오.")
    path = src.file_path
    audit.record(db, user, "knowledge_delete", "knowledge", src.id, {"title": src.title, "sha256": src.sha256}, ip)
    db.delete(src)
    db.flush()
    if path and Path(path).exists():
        Path(path).unlink()


# ================================================================== 과제·심의 선례
def _labels(catalog: str, keys) -> str:
    rs = get_ruleset()
    return ", ".join(rs.label(catalog, k) for k in (keys or []))


def protocol_text(p: Protocol) -> tuple[str, str]:
    """색인할 과제 본문(최근 제출 스냅샷 기준). (heading, text)"""
    rs = get_ruleset()
    sub = p.current_submission
    snap = sub.snapshot if sub and sub.snapshot else {}

    def g(key: str):
        return snap.get(key, getattr(p, key, None)) if snap else getattr(p, key, None)

    lines = [
        f"연구 제목: {g('title')}",
        f"연구 유형: {rs.label('study_types', g('study_type') or '')}",
        f"연구 목적: {g('objectives') or ''}",
        f"연구 방법: {g('methods') or ''}",
        f"연구 대상: {g('target_population') or ''}",
        f"선정 기준: {g('inclusion_criteria') or ''}",
        f"데이터 출처: {_labels('data_sources', g('data_sources'))}",
        f"처리 수준: {rs.label('deid_levels', g('deid_level') or '')}",
        f"민감정보: {_labels('sensitive_categories', g('sensitive_categories')) or '없음'}",
        f"취약한 연구대상자: {_labels('vulnerable_populations', g('vulnerable_populations')) or '없음'}",
        f"비정형 데이터: {_labels('unstructured_data', g('unstructured_data')) or '없음'}",
        f"동의: {'서면동의 취득' if g('consent_plan') == 'obtain' else '동의 면제 신청'}",
        f"심의 유형: {p.review_type.label if p.review_type else '-'} · 상태: {p.status.label}",
    ]
    return "과제 요약", "\n".join(lines)


def decision_text(d: Decision) -> tuple[str, str, str]:
    p = d.submission.protocol
    title = f"[{d.decision.label}] {p.protocol_no} {d.submission.snapshot.get('title') or p.title}"
    lines = [
        f"심의 결정: {d.decision.label} ({d.review_type.label}, {d.submission.kind.label})",
        f"결정일: {d.decided_at:%Y-%m-%d}" + (f" · 회의일: {d.meeting_date}" if d.meeting_date else ""),
    ]
    if d.conditions:
        lines.append(f"시정 조건·보완 사유: {d.conditions}")
    if d.comments:
        lines.append(f"위원회 의견: {d.comments}")
    pr = d.submission.latest_prereview
    if pr:
        majors = [f["message"] for f in pr.result.get("findings", []) if f["severity"] in ("critical", "major")]
        if majors:
            lines.append("주요 검토 의견: " + " / ".join(majors[:6]))
    return title, "심의 결정", "\n".join(lines)


_sync_memo: dict[str, Any] = {}


def _engine_key(db: Session) -> str:
    return str(db.get_bind().url)


def _precedent_marker(db: Session) -> tuple:
    p = db.execute(select(func.count(Protocol.id), func.max(Protocol.updated_at))
                   .where(Protocol.protocol_no.is_not(None))).one()
    d = db.execute(select(func.count(Decision.id), func.max(Decision.id))).one()
    return (p[0], str(p[1]), d[0], d[1])


def reindex_protocols(db: Session, force: bool = False) -> dict[str, int]:
    """제출된 과제와 심의 결정을 선례 지식으로 색인(변경된 것만 갱신)."""
    key = _engine_key(db)
    marker = _precedent_marker(db)
    if not force and _sync_memo.get(key) == marker:
        return {"protocols": 0, "decisions": 0}
    stats = {"protocols": 0, "decisions": 0}
    existing = {s.key: s for s in db.scalars(
        select(KnowledgeSource).where(KnowledgeSource.kind.in_(["protocol", "decision"])))}
    seen: set[str] = set()
    for p in db.scalars(select(Protocol).where(Protocol.protocol_no.is_not(None))):
        heading, text = protocol_text(p)
        sig = hashlib.sha1(json.dumps([text, p.status.value], ensure_ascii=False).encode()).hexdigest()
        skey = f"protocol:{p.id}"
        seen.add(skey)
        src = existing.get(skey)
        if src is None:
            src = KnowledgeSource(key=skey, kind="protocol", title="", meta={})
            db.add(src)
        if force or src.meta.get("signature") != sig or src.title != f"{p.protocol_no} {p.title}":
            src.title = f"{p.protocol_no} {p.title}"[:300]
            src.ref = p.protocol_no or ""
            src.url = f"/protocols/{p.id}"
            src.meta = {"protocol_id": p.id, "signature": sig}
            db.flush()
            _set_chunks(db, src, [(heading, text)])
            stats["protocols"] += 1
    decisions = db.scalars(select(Decision).join(Submission)).all()
    for d in decisions:
        skey = f"decision:{d.id}"
        seen.add(skey)
        if skey in existing and not force:
            continue
        title, heading, text = decision_text(d)
        src = existing.get(skey) or KnowledgeSource(key=skey, kind="decision", title="", meta={})
        if src.id is None:
            db.add(src)
        p = d.submission.protocol
        src.title = title[:300]
        src.ref = d.letter_no
        src.url = f"/protocols/{p.id}#review"
        src.meta = {"protocol_id": p.id, "decision_id": d.id, "decision": d.decision.value}
        db.flush()
        _set_chunks(db, src, [(heading, text)])
        stats["decisions"] += 1
    for skey, src in existing.items():
        if skey not in seen:
            db.delete(src)
    db.flush()
    _sync_memo[key] = _precedent_marker(db)
    return stats


def index_stamp(db: Session) -> tuple:
    """색인 캐시 무효화 표식: 청크·출처 수, 최대 id, 최근 변경 시각, 활성 출처 수."""
    c = db.execute(select(func.count(KnowledgeChunk.id), func.max(KnowledgeChunk.id))).one()
    s = db.execute(select(func.count(KnowledgeSource.id), func.max(KnowledgeSource.updated_at))).one()
    active = db.scalar(select(func.count(KnowledgeSource.id)).where(KnowledgeSource.active.is_(True)))
    return (c[0], c[1], s[0], str(s[1]), active)
