"""서류 품질 점검 실행·저장·자동 교정.

  업로드/과제 서류 → 텍스트 인식 → Phase 1 규칙(필수항목·버전·용어·미기입·교차/과제 정보 대조·규제)
                    → Phase 2 AI 심층(선택) → Phase 3 오탈자·숫자·날짜 → 점수(100 - 15C - 7M - 2m) → PASS/FAIL
  → 이슈 선택 → 자동 교정(변경 추적본·수정 완료본) → 새 버전 저장 → 재점검
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..models import Document, Protocol, User
from ..timeutil import now
from . import autofix
from .checks import QCDoc, criteria, run_rules
from .models import QCRun

log = logging.getLogger(__name__)
SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}


class QCError(Exception):
    pass


def qc_dir() -> Path:
    d = get_settings().data_dir / "qc"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ 문서 준비
def _read_doc(key: str, name: str, raw: bytes, doc_type: str, document_id: int | None = None) -> QCDoc:
    from ..docproc.text import detect_format, extract_text

    ex = extract_text(name, raw)
    fmt = detect_format(name, raw)
    text = ex.text or ""
    extra = _header_footer_text(raw) if fmt == "docx" else ""
    if extra:
        text = f"{extra}\n{text}\n{extra}"  # 머리글·바닥글(표지 버전 표기 등)도 점검 대상
    return QCDoc(key=key, name=name, doc_type=doc_type, text=text, document_id=document_id,
                 fmt=fmt, warnings=list(ex.warnings or []))


def _header_footer_text(raw: bytes) -> str:
    try:
        from ..doctemplates.docx_engine import _roots, load
        from .autofix import w

        roots = _roots(load(raw))[1:]
    except Exception:  # 손상 파일 등은 본문 추출 결과만 사용
        return ""
    lines = []
    for root in roots:
        for p in root.iter(w("p")):
            t = "".join(x.text or "" for x in p.iter(w("t"))).strip()
            if t and t not in lines:
                lines.append(t)
    return "\n".join(lines)


def _guess(name: str, raw: bytes) -> str:
    from ..submission.guess import guess_doc_type

    g = guess_doc_type(name, raw)
    return g.doc_type if g.confidence >= 0.5 else "other"


def protocol_documents(protocol: Protocol) -> list[Document]:
    from ..submission.versions import latest_by_type

    latest = latest_by_type(list(protocol.documents))
    return [d for d in latest.values() if d.stored_path and Path(d.stored_path).is_file()]


def _summary(protocol: Protocol | None) -> str:
    if protocol is None:
        return ""
    from ..engine.ruleset import get_ruleset

    rs = get_ruleset()
    lines = [
        f"제목: {protocol.title}",
        f"연구책임자: {protocol.pi.full_name if protocol.pi else ''}",
        f"연구 유형: {rs.label('study_types', protocol.study_type)}",
        f"연구 기간: {protocol.study_start or '?'} ~ {protocol.study_end or '?'}",
        f"자료 기간: {protocol.data_period_start or '?'} ~ {protocol.data_period_end or '?'}",
        f"대상자 수: {protocol.expected_subjects:,}명",
        f"처리 수준: {rs.label('deid_levels', protocol.deid_level)}",
        f"동의: {'면제 신청' if protocol.consent_plan == 'waiver' else '동의 취득'}",
        f"보관 기간: {protocol.retention_years}년",
        f"취약 대상: {', '.join(protocol.vulnerable_populations or []) or '없음'}",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ 점수
def score(issues: list[dict]) -> tuple[int, str, dict[str, int]]:
    sc = criteria().get("scoring", {})
    counts = Counter(i["severity"] for i in issues)
    total = 100 - counts["critical"] * int(sc.get("critical", 15)) - counts["major"] * int(sc.get("major", 7)) \
        - counts["minor"] * int(sc.get("minor", 2))
    total = max(0, total)
    fail = (counts["critical"] > 0 and sc.get("critical_fails", True)) or total < int(sc.get("pass_score", 80))
    return total, "FAIL" if fail else "PASS", {k: counts.get(k, 0) for k in ("critical", "major", "minor")}


# ------------------------------------------------------------------ 실행
def run(db: Session, user: User, docs: list[QCDoc], *, protocol: Protocol | None = None, use_ai: bool = True,
        title: str = "", ip: str = "", store: dict[str, str] | None = None) -> QCRun:
    if not docs:
        raise QCError("점검할 서류가 없습니다.")
    p1, p3 = run_rules(docs, protocol)
    qr = QCRun(protocol_id=protocol.id if protocol else None, created_by=user.id, title=title[:200],
               criteria_version=str(criteria().get("version", "")))
    db.add(qr)
    db.flush()
    ai_issues: list[dict] = []
    ai_info: dict[str, Any] = {"status": "skipped", "message": "요청하지 않음"}
    if use_ai:
        from .ai_phase import run_ai

        ai_issues, ai_info = run_ai(docs, p1 + p3, _summary(protocol), db=db, user=user, entity_id=qr.id)
    issues = sorted(p1 + ai_issues + p3, key=lambda i: (SEVERITY_ORDER.get(i["severity"], 9), i["phase"], i["doc"] or ""))
    for n, it in enumerate(issues, start=1):
        it["id"] = n
    total, verdict, counts = score(issues)
    qr.issues = issues
    qr.score, qr.verdict, qr.counts = total, verdict, counts
    qr.ai_used = ai_info.get("status") == "done"
    qr.phases = {
        "1": {"label": "규칙 기반 스캔", "status": "done", "issues": len(p1)},
        "2": {"label": "AI 심층 분석", "status": ai_info.get("status"), "issues": len(ai_issues),
              "message": ai_info.get("message", "")},
        "3": {"label": "오탈자·숫자·날짜 검증", "status": "done", "issues": len(p3)},
    }
    by_doc = Counter(i["doc"] for i in issues)
    qr.docs = [{
        "key": d.key, "name": d.name, "doc_type": d.doc_type, "label": d.label, "document_id": d.document_id,
        "path": (store or {}).get(d.key, ""), "fmt": d.fmt, "chars": len(d.text), "warnings": d.warnings,
        "fixable": d.fmt in ("docx", "hwpx", "text"), "issues": by_doc.get(d.key, 0),
        "facts": [{"key": f.key, "value": f.value, "raw": f.raw} for f in d.facts][:40],
    } for d in docs]
    if protocol is not None:
        for d in docs:
            if d.document_id:
                doc = db.get(Document, d.document_id)
                if doc is not None:
                    sev = Counter(i["severity"] for i in issues if i["doc"] == d.key)
                    doc.validation = {**(doc.validation or {}), "qc": {
                        "run": qr.id, "critical": sev["critical"], "major": sev["major"], "minor": sev["minor"],
                        "at": now().isoformat(timespec="minutes")}}
    audit.record(db, user, "qc_run", "protocol" if protocol else "qc_run", protocol.id if protocol else qr.id,
                 {"run": qr.id, "score": total, "verdict": verdict, **counts, "docs": len(docs), "ai": qr.ai_used}, ip)
    return qr


def run_for_protocol(db: Session, user: User, protocol: Protocol, *, doc_ids: list[int] | None = None,
                     use_ai: bool = True, ip: str = "") -> QCRun:
    docs = protocol_documents(protocol)
    if doc_ids:
        docs = [d for d in docs if d.id in set(doc_ids)]
    qdocs = [_read_doc(f"d{i}", d.filename, Path(d.stored_path).read_bytes(), d.doc_type, d.id)
             for i, d in enumerate(docs, start=1)]
    store = {f"d{i}": d.stored_path for i, d in enumerate(docs, start=1)}
    return run(db, user, qdocs, protocol=protocol, use_ai=use_ai, ip=ip, store=store,
               title=f"{protocol.title[:60]} 서류 {len(qdocs)}건")


def run_uploads(db: Session, user: User, files: list[tuple[str, bytes, str | None]], *, use_ai: bool = True,
                protocol: Protocol | None = None, ip: str = "") -> QCRun:
    """빠른 점검: 과제와 무관하게 파일을 올려 점검(과제를 지정하면 과제 정보와도 대조)."""
    from ..submission.validate import validate_upload

    qdocs, store, rejected = [], {}, []
    folder = qc_dir() / uuid.uuid4().hex
    for i, (name, raw, doc_type) in enumerate(files, start=1):
        v = validate_upload(name, raw, max_bytes=get_settings().max_upload_mb * 1024 * 1024)
        if not v.ok:
            rejected.append(f"{name}: {' '.join(v.errors)}")
            continue
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{i}_{v.safe_name}"
        path.write_bytes(raw)
        key = f"d{i}"
        store[key] = str(path)
        qdocs.append(_read_doc(key, Path(name).name, raw, doc_type or _guess(name, raw)))
    if not qdocs:
        raise QCError("점검할 수 있는 파일이 없습니다. " + " / ".join(rejected))
    qr = run(db, user, qdocs, protocol=protocol, use_ai=use_ai, ip=ip, store=store,
             title=", ".join(d.name for d in qdocs)[:200])
    if rejected:
        qr.phases = {**qr.phases, "rejected": rejected}
    return qr


# ------------------------------------------------------------------ 자동 교정
def fixable_issues(qr: QCRun) -> list[dict]:
    return [i for i in qr.issues if i.get("fixes") and any((qr.doc(f["doc"]) or {}).get("fixable") for f in i["fixes"])]


def apply_fixes(db: Session, user: User, qr: QCRun, issue_ids: list[int], *, font: str | None = None,
                save_version: bool = True, ip: str = "") -> list[dict]:
    """선택한 이슈의 교정을 문서별로 적용. 문서마다 변경 추적본과 수정 완료본을 만든다.

    과제 서류는 수정 완료본을 새 버전으로 저장(save_version)한다. 반환: 출력 파일 목록.
    """
    chosen = [qr.issue(i) for i in issue_ids]
    chosen = [c for c in chosen if c and c.get("fixes")]
    per_doc: dict[str, list[dict]] = {}
    for it in chosen:
        for fx in it["fixes"]:
            per_doc.setdefault(fx["doc"], []).append({**fx, "issue": it["id"]})
    if font:
        for d in qr.docs:
            if d["fixable"]:
                per_doc.setdefault(d["key"], [])
    if not per_doc:
        raise QCError("자동 교정할 항목을 선택하십시오(자동 교정 가능 표시가 있는 이슈만 가능).")
    outputs: list[dict] = []
    folder = qc_dir() / f"run{qr.id}"
    folder.mkdir(parents=True, exist_ok=True)
    protocol = qr.protocol
    for key, fixes in per_doc.items():
        d = qr.doc(key)
        if not d or not d.get("path") or not Path(d["path"]).is_file():
            continue
        if not d.get("fixable"):
            outputs.append({"doc": key, "name": d["name"], "error": "DOCX·HWPX·TXT 원본만 자동 교정할 수 있습니다."})
            continue
        raw = Path(d["path"]).read_bytes()
        stem, ext = Path(d["name"]).stem, Path(d["name"]).suffix
        try:
            clean, rep, _ = autofix.apply(d["name"], raw, fixes, mode="clean", font=font, highlight=True)
            tracked = None
            if d["fmt"] == "docx":
                tracked, _, _ = autofix.apply(d["name"], raw, fixes, mode="tracked", font=font)
        except autofix.AutofixError as e:
            outputs.append({"doc": key, "name": d["name"], "error": str(e)})
            continue
        token = uuid.uuid4().hex[:8]
        entry: dict[str, Any] = {"doc": key, "name": d["name"], "applied": rep.total, "font": font,
                                 "issues": sorted({f["issue"] for f in fixes if "issue" in f}),
                                 "missed": [fixes[i]["old"] for i, n in rep.applied.items() if n == 0]}
        clean_path = folder / f"{token}_clean{ext}"
        clean_path.write_bytes(clean)
        entry["clean"] = {"path": str(clean_path), "filename": f"{stem}_fixed{ext}",
                          "sha256": hashlib.sha256(clean).hexdigest()}
        if tracked is not None:
            tp = folder / f"{token}_tracked{ext}"
            tp.write_bytes(tracked)
            entry["tracked"] = {"path": str(tp), "filename": f"{stem}_변경추적{ext}"}
        if save_version and protocol is not None and d.get("document_id") and rep.total:
            from ..submission import service as subs

            src = db.get(Document, d["document_id"])
            doc_type = src.doc_type if src else d["doc_type"]
            try:
                newdoc, _ = subs.store_document(db, user, protocol, doc_type=doc_type,
                                                filename=_versioned_name(d["name"]), raw=clean,
                                                source="autofix", ip=ip, run_hook=False)
                entry["document_id"] = newdoc.id
                entry["version"] = newdoc.version
            except subs.SubmissionError as e:
                entry["save_error"] = str(e)
        outputs.append(entry)
    qr.outputs = [*(qr.outputs or []), *outputs]
    audit.record(db, user, "qc_autofix", "protocol" if protocol else "qc_run", protocol.id if protocol else qr.id,
                 {"run": qr.id, "issues": sorted({i["id"] for i in chosen}), "font": font,
                  "outputs": [{k: v for k, v in o.items() if k in ("name", "applied", "version", "error")} for o in outputs]},
                 ip)
    return outputs


def _versioned_name(name: str) -> str:
    """파일명에 버전이 있으면 그대로(버전은 연구자가 정함), 없으면 '_교정' 표시."""
    from .facts import version_from_filename

    if version_from_filename(name):
        return name
    p = Path(name)
    return f"{p.stem}_교정{p.suffix}"


def output_file(qr: QCRun, token_path: str) -> Path | None:
    for o in qr.outputs or []:
        for kind in ("clean", "tracked"):
            if o.get(kind, {}).get("path") == token_path:
                return Path(token_path)
    return None
