"""서류 품질 점검 실행·저장·자동 교정.

  업로드/과제 서류 → 텍스트 인식 → Phase 1 규칙(필수항목·버전·용어·미기입·교차/과제 정보 대조·규제)
                    → 규칙 지적 AI 재검토(선택) → Phase 2 AI 심층(선택) → Phase 3 오탈자·숫자·날짜 → 검토·점수 및 QC 판정
  → 이슈 선택 → 자동 교정(변경 추적본·수정 완료본) → 새 버전 저장 → 재점검
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..models import Document, Protocol, User
from . import autofix
from .bundle import inspect_bundle, mixed_study_findings
from .checks import QCDoc, criteria, run_rules
from .grouping import group_issues, merge_ai_evidence
from .models import QCRun
from .pattern_store import ClassificationPatterns, active_classification, active_required, use_database_settings
from .profile import Answers, build_profile, profile_conditions
from .review import refresh
from .rules import rules
from .units import annotate, observed_units

log = logging.getLogger(__name__)
SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}


class QCError(Exception):
    pass


def qc_dir() -> Path:
    d = get_settings().data_dir / "qc"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ 문서 준비
def _read_doc(key: str, name: str, raw: bytes, doc_type: str, document_id: int | None = None,
              *, allow_ai_ocr: bool = False) -> QCDoc:
    from ..docproc.text import detect_format, extract_text

    ex = extract_text(name, raw, allow_ai_ocr=allow_ai_ocr)
    fmt = detect_format(name, raw)
    text = ex.text or ""
    extra = _header_footer_text(raw) if fmt == "docx" else ""
    if extra:
        text = f"{text}\n[QC: 머리글·바닥글]\n{extra}" if text.strip() else ""
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


def _guess(name: str, text: str, patterns: ClassificationPatterns) -> str:
    from ..submission.guess import guess_doc_type

    g = guess_doc_type(name, extracted_text=text, patterns=patterns)
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


# ------------------------------------------------------------------ 실행
def run(db: Session, user: User, docs: list[QCDoc], *, protocol: Protocol | None = None, use_ai: bool = True,
        title: str = "", ip: str = "", store: dict[str, str] | None = None, profile: Answers | None = None,
        qr: QCRun | None = None, on_stage: Callable[[str], None] | None = None,
        score_run: Callable[[QCRun], None] = refresh) -> QCRun:
    with use_database_settings(db):
        if not docs:
            raise QCError("점검할 서류가 없습니다.")
        if on_stage:
            on_stage("규칙 점검 중")
        applied = build_profile(profile, protocol, docs)
        bundle_units, bundle_issues = ([], []) if applied.get("compare_only") else inspect_bundle(docs, applied, protocol)
        for condition, rule_ids in (("drug", ("M02", "M03")), ("visits", ("M04",))):
            if applied.get(condition) is False:
                bundle_units.extend({"rule": rid, "field": condition, "scope": "제출 묶음", "docs": [],
                                     "result": "na", "reason": "연구 조건에 해당하지 않습니다."} for rid in rule_ids)
        from .feedback import active_aliases, capture_rule_feedback

        p1, p3 = run_rules(docs, protocol, profile_conditions(applied), applied, units=bundle_units,
                           aliases=active_aliases(db), patterns=active_required(db))
        mix = [] if applied.get("compare_only") else mixed_study_findings(docs)
        p1 = bundle_issues + mix + p1
        rule_issues = [annotate(issue) for issue in p1 + p3]
        if qr is None:
            qr = QCRun(protocol_id=protocol.id if protocol else None, created_by=user.id, title=title[:200])
            db.add(qr)
            db.flush()
        ai_issues: list[dict] = []
        ai_info: dict[str, Any] = {"status": "skipped", "message": "요청하지 않음"}
        verify_info: dict[str, Any] = {"status": "skipped", "message": "요청하지 않음"}
        if use_ai:
            from .ai_phase import run_ai
            from .ai_verify import verify_rule_findings

            if on_stage:
                on_stage("AI 심층 분석 중")
            ai_issues, ai_info = run_ai(docs, rule_issues, _summary(protocol), db=db, user=user, entity_id=qr.id)
            ai_issues = [annotate(issue) for issue in merge_ai_evidence(rule_issues, ai_issues)]
            if on_stage:
                on_stage("원문 근거 최종 AI 재검토 중")
            verify_info = verify_rule_findings(docs, rule_issues + ai_issues, db=db, user=user, entity_id=qr.id)
        if on_stage:
            on_stage("점수 계산 중")
        qr.criteria_version = f"{criteria().get('version', '')}+r{rules()['version']}"[:32]
        qr.profile = applied
        issues = sorted(rule_issues + ai_issues, key=lambda i: (SEVERITY_ORDER.get(i["severity"], 9), i["phase"], i["doc"] or ""))
        auto = criteria().get("review", {}).get("auto_confirm", [])
        for n, it in enumerate(issues, start=1):
            it["id"] = n
            if (it["confidence"] == "rule" and it["review"] == "candidate" and it["result"] != "needs_check" and not it.get("advice")
                    and (it.get("rule_id") in auto or set(it.get("flags", [])) & set(auto))):
                it["review"] = "confirmed"
                it["review_note"] = "기관 설정에 따라 자동 확정"
        qr.issues = issues
        group_issues(issues)
        qr.checks = observed_units(docs, issues, bundle_units)
        qr.ai_used = ai_info.get("status") in ("done", "partial")
        qr.phases = {
            "1": {"label": "규칙 기반 스캔", "status": "done", "issues": len(p1)},
            "verify": {"label": "원문 근거 최종 AI 재검토", **verify_info, "issues": verify_info.get("dismissed", 0)},
            "2": {"label": "AI 심층 분석", "status": ai_info.get("status"), "issues": len(ai_issues),
                  "message": ai_info.get("message", "")},
            "3": {"label": "오탈자·숫자·날짜 검증", "status": "done", "issues": len(p3)},
        }
        by_doc = Counter(i["doc"] for i in issues if i["review"] != "dismissed" and not i.get("merged_into"))
        qr.docs = [{
            "key": d.key, "name": d.name, "doc_type": d.doc_type, "label": d.label, "document_id": d.document_id,
            "path": (store or {}).get(d.key, ""), "fmt": d.fmt, "chars": len(d.text.strip()), "warnings": d.warnings,
            "fixable": d.fmt in ("docx", "hwpx", "text"), "issues": by_doc.get(d.key, 0),
            "facts": [{"key": f.key, "value": f.value, "raw": f.raw} for f in d.facts][:40],
        } for d in docs]
        score_run(qr)
        capture_rule_feedback(db, qr)
        audit.record(db, user, "qc_run", "protocol" if protocol else "qc_run", protocol.id if protocol else qr.id,
                     {"run": qr.id, "score": qr.score, "verdict": qr.verdict, **qr.counts, "docs": len(docs), "ai": qr.ai_used}, ip)
        return qr


def run_for_protocol(db: Session, user: User, protocol: Protocol, *, doc_ids: list[int] | None = None,
                     use_ai: bool = True, ip: str = "", profile: Answers | None = None) -> QCRun:
    docs = protocol_documents(protocol)
    if doc_ids:
        docs = [d for d in docs if d.id in set(doc_ids)]
        profile = {**(profile or {}), "compare_only": True}
    qdocs = [_read_doc(f"d{i}", d.filename, Path(d.stored_path).read_bytes(), d.doc_type, d.id)
             for i, d in enumerate(docs, start=1)]
    store = {f"d{i}": d.stored_path for i, d in enumerate(docs, start=1)}
    return run(db, user, qdocs, protocol=protocol, use_ai=use_ai, ip=ip, store=store, profile=profile,
               title=f"{protocol.title[:60]} 서류 {len(qdocs)}건")


def run_uploads(db: Session, user: User, files: list[tuple[str, bytes, str | None]], *, use_ai: bool = True,
                protocol: Protocol | None = None, ip: str = "", profile: Answers | None = None) -> QCRun:
    """빠른 점검: 과제와 무관하게 파일을 올려 점검(과제를 지정하면 과제 정보와도 대조)."""
    from ..submission.validate import validate_upload

    qdocs, store, rejected = [], {}, []
    patterns = active_classification(db)
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
        doc = _read_doc(key, Path(name).name, raw, doc_type or "other")
        if not doc_type:
            doc.doc_type = _guess(doc.name, doc.text, patterns)
        qdocs.append(doc)
    if not qdocs:
        raise QCError("점검할 수 있는 파일이 없습니다. " + " / ".join(rejected))
    qr = run(db, user, qdocs, protocol=protocol, use_ai=use_ai, ip=ip, store=store, profile=profile,
             title=", ".join(d.name for d in qdocs)[:200])
    if rejected:
        qr.phases = {**qr.phases, "rejected": rejected}
    return qr


def rerun_saved(db: Session, user: User, previous: QCRun, *, profile: Answers, ip: str = "") -> QCRun:
    """Repeat the exact saved bundle with edited answers."""
    docs, store = [], {}
    for entry in previous.docs:
        path = Path(entry.get("path") or "")
        if not path.is_file():
            raise QCError("저장된 서류를 읽을 수 없습니다. 원본을 다시 업로드해 주세요.")
        store[entry["key"]] = str(path)
        docs.append(_read_doc(entry["key"], entry["name"], path.read_bytes(), entry["doc_type"], entry["document_id"]))
    return run(db, user, docs, protocol=previous.protocol, use_ai=previous.ai_used, ip=ip, store=store,
               profile=profile, title=previous.title)


# ------------------------------------------------------------------ 자동 교정
def fixable_issues(qr: QCRun) -> list[dict]:
    return [i for i in qr.issues if i.get("fixes") and i.get("result") != "needs_check"
            and i.get("review") not in ("dismissed", "resolved", "accepted") and not i.get("merged_into")
            and (i.get("category") != "cross" or i.get("reference"))
            and any((qr.doc(f["doc"]) or {}).get("fixable") for f in i["fixes"])]


def apply_fixes(db: Session, user: User, qr: QCRun, issue_ids: list[int], *, font: str | None = None,
                save_version: bool = True, ip: str = "") -> list[dict]:
    """선택한 이슈의 교정을 문서별로 적용. 문서마다 변경 추적본과 수정 완료본을 만든다.

    과제 서류는 수정 완료본을 새 버전으로 저장(save_version)한다. 반환: 출력 파일 목록.
    """
    chosen = [i for i in fixable_issues(qr) if i["id"] in issue_ids]
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
