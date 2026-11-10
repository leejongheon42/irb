"""규칙 기반 점검(Phase 1: 필수항목·버전·용어·미기입·교차/과제 정보 대조·규제 / Phase 3: 오탈자·숫자·날짜).

모든 점검은 결정론적이다(같은 입력 → 같은 결과). 이슈는 dict로 반환하며 fix가 있으면 자동 교정 대상이다.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from ..config import get_settings
from . import facts as F

CRITERIA_PATH = Path(__file__).with_name("criteria.yaml")


@lru_cache(maxsize=4)
def _load(path: str, mtime: float) -> dict[str, Any]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def criteria() -> dict[str, Any]:
    path = Path(getattr(get_settings(), "qc_criteria_path", "") or CRITERIA_PATH)
    if not path.exists():
        path = CRITERIA_PATH
    return _load(str(path), path.stat().st_mtime)


@dataclass
class QCDoc:
    """점검 대상 문서 1건."""

    key: str  # 실행 안에서의 식별자(d1, d2, ...)
    name: str
    doc_type: str
    text: str
    document_id: int | None = None
    fmt: str = ""
    warnings: list[str] = field(default_factory=list)
    facts: list[F.Fact] = field(default_factory=list)

    @property
    def label(self) -> str:
        from ..engine.ruleset import get_ruleset

        return get_ruleset().label("document_types", self.doc_type)


def _issue(severity: str, category: str, title: str, *, doc: QCDoc | None = None, detail: str = "",
           evidence: str = "", suggestion: str = "", fixes: list[dict] | None = None, phase: int = 1,
           source: str = "rule", code: str = "") -> dict[str, Any]:
    return {
        "severity": severity, "category": category, "title": title, "detail": detail,
        "doc": doc.key if doc else None, "doc_name": doc.name if doc else "", "evidence": evidence[:300],
        "suggestion": suggestion, "fixes": fixes or [], "phase": phase, "source": source, "code": code,
    }


def _match(text: str, pattern: str) -> re.Match[str] | bool | None:
    if pattern.startswith("re:"):
        return re.search(pattern[3:], text, re.I)
    return pattern.lower() in text.lower()


# ------------------------------------------------------------------ 조건
def conditions(protocol: Any | None) -> set[str]:
    if protocol is None:
        return set()
    c: set[str] = set()
    if "children" in (protocol.vulnerable_populations or []):
        c.add("children")
    c.add("waiver" if protocol.consent_plan == "waiver" else "obtain")
    if protocol.deid_level == "identifiable":
        c.add("identifiable")
    if protocol.study_type in ("prospective_observational",) or not protocol.existing_data_only:
        c.add("prospective")
    if getattr(protocol, "ai_ml_development", False):
        c.add("ai_ml")
    if protocol.external_transfer:
        c.add("external_transfer")
    return c


# ------------------------------------------------------------------ Phase 1
def check_required(doc: QCDoc, conds: set[str]) -> list[dict]:
    out = []
    for item in criteria().get("required", {}).get(doc.doc_type, []):
        when = set(item.get("when") or [])
        if when and not (when & conds):
            continue
        ok = True
        if item.get("all"):
            ok = all(_match(doc.text, p) for p in item["all"])
        if ok and item.get("any"):
            ok = any(_match(doc.text, p) for p in item["any"])
        if not ok:
            out.append(_issue(
                item.get("severity", "major"), "required", f"필수 기재사항 누락: {item['label']}", doc=doc,
                detail=f"{doc.label}에 '{item['label']}' 관련 내용이 확인되지 않습니다.",
                suggestion=f"'{item['label']}' 항목을 추가하십시오. 자동 교정 대상이 아니며 직접 작성해야 합니다.",
                code=f"REQ_{doc.doc_type}_{item['id']}".upper()))
    return out


def check_version(doc: QCDoc) -> list[dict]:
    out = []
    fv = F.version_from_filename(doc.name)
    body = F.versions_in_body(doc.text)
    body_vals = sorted({b.value for b in body})
    if fv and body_vals and fv not in body_vals:
        wrong = next(b for b in body if b.value != fv)
        fixes = []
        for raw in dict.fromkeys(b.raw for b in body if b.value != fv):
            fixes.append({"doc": doc.key, "old": raw, "new": re.sub(r"\d+(?:\.\d+){0,2}", _ver_like(raw, fv), raw, count=1),
                          "all": True})
        out.append(_issue(
            "critical", "version", "파일명과 본문의 버전이 일치하지 않습니다", doc=doc,
            detail=f"파일명 v{_ver_like(wrong.raw, fv)} ≠ 본문 {', '.join(dict.fromkeys(b.raw for b in body if b.value != fv))}",
            evidence=wrong.line,
            suggestion="본문(표지·머리글·바닥글) 버전 표기를 파일명과 같게 고치거나, 실제 최신본인지 확인 후 파일명을 바꾸십시오.",
            fixes=fixes, code="VERSION_MISMATCH"))
    elif len(body_vals) > 1:
        out.append(_issue(
            "major", "version", "본문 안에 서로 다른 버전 표기가 있습니다", doc=doc,
            detail="본문 버전: " + ", ".join("v" + v for v in body_vals), evidence=body[0].line,
            suggestion="표지·머리글·바닥글의 버전 표기를 하나로 통일하십시오.", code="VERSION_INCONSISTENT"))
    elif fv is None and not body_vals and doc.doc_type in ("protocol", "consent_form"):
        out.append(_issue(
            "minor", "version", "버전 표기가 없습니다", doc=doc,
            suggestion="IRB 제출 서류는 파일명과 표지에 버전·작성일을 표기하는 것이 원칙입니다(예: v1.0, 2026.11.10).",
            code="VERSION_MISSING"))
    return out


def _ver_like(raw: str, v: str) -> str:
    """원문 표기 자릿수(3.0 / 3)를 유지한 버전 문자열."""
    m = re.search(r"\d+(?:\.\d+){0,2}", raw)
    parts = v.split(".")
    if m:
        width = m.group(0).count(".") + 1
        while len(parts) < width:
            parts.append("0")
    return ".".join(parts)


def check_terminology(docs: list[QCDoc]) -> list[dict]:
    out = []
    for group in criteria().get("terminology", []):
        preferred = group["preferred"]
        used_pref = {d.key for d in docs if preferred in d.text}
        for d in docs:
            variants = [v for v in group.get("variants", []) if v in _without(d.text, preferred, v)]
            if not variants:
                continue
            mixed = d.key in used_pref or bool(used_pref - {d.key})
            counts = {v: _count(d.text, v, preferred) for v in variants}
            out.append(_issue(
                "major" if mixed else "minor", "terminology",
                f"용어 통일 필요: '{'·'.join(variants)}' → '{preferred}'", doc=d,
                detail=("같은 서류 또는 제출 패키지 안에서 표준 용어와 혼용되고 있습니다." if mixed
                        else "기관 표준 용어로 바꾸는 것을 권장합니다.")
                + " (" + ", ".join(f"{v} {n}회" for v, n in counts.items()) + ")",
                evidence=_context(d.text, variants[0]),
                suggestion=f"'{preferred}'(으)로 통일하십시오.",
                fixes=[{"doc": d.key, "old": v, "new": preferred, "all": True} for v in variants],
                code="TERM_" + preferred))
    return out


def _without(text: str, preferred: str, variant: str) -> str:
    """표준 용어 안에 변형이 포함되는 경우(예: '연구 대상자' vs '연구대상자')를 구분하기 위해 표준 용어를 제거."""
    return text.replace(preferred, "\0") if variant in preferred else text


def _count(text: str, variant: str, preferred: str) -> int:
    return _without(text, preferred, variant).count(variant)


def _context(text: str, needle: str, width: int = 60) -> str:
    i = text.find(needle)
    if i < 0:
        return ""
    return text[max(0, i - width):i + len(needle) + width].replace("\n", " ").strip()


def check_placeholders(doc: QCDoc) -> list[dict]:
    found = []
    for p in criteria().get("placeholders", []):
        if p.startswith("re:"):
            found += [m.group(0) for m in re.finditer(p[3:], doc.text)]
        elif p in doc.text:
            found.append(p)
    found = list(dict.fromkeys(found))
    if not found:
        return []
    return [_issue(
        "major", "placeholder", f"미기입 항목·자리표시자 {len(found)}건", doc=doc,
        detail="양식의 빈칸이나 자리표시자가 그대로 남아 있습니다: " + ", ".join(found[:8]),
        evidence=_context(doc.text, found[0]), suggestion="해당 칸을 실제 내용으로 채우십시오(자동 완성에서 과제 정보를 먼저 입력하면 채워집니다).",
        code="PLACEHOLDER")]


def check_pii(doc: QCDoc) -> list[dict]:
    try:
        from ..ai.privacy import scan
    except ImportError:  # pragma: no cover
        return []
    counts = scan(doc.text)
    severe = {k: v for k, v in counts.items() if k in ("rrn", "foreigner_no", "passport", "card", "account")}
    if not severe:
        return []
    return [_issue(
        "critical", "regulatory", "서류에 실제 개인정보가 포함되어 있습니다", doc=doc,
        detail=", ".join(f"{k} {v}건" for k, v in severe.items()),
        suggestion="예시·샘플 자료에는 가상의 값을 쓰고, 실제 환자 정보는 제출 서류에서 삭제하십시오.", code="PII_IN_DOC")]


def check_regulatory(doc: QCDoc, protocol: Any | None) -> list[dict]:
    out = []
    min_years = int(criteria().get("regulatory", {}).get("min_retention_years", 3))
    for f in doc.facts:
        if f.key == "retention" and int(f.value) < min_years:
            out.append(_issue(
                "major", "regulatory", f"자료 보관 기간이 {min_years}년 미만입니다", doc=doc, evidence=f.line,
                detail=f"생명윤리법 시행규칙 제15조는 연구 관련 기록을 연구 종료 후 {min_years}년간 보관하도록 정하고 있습니다.",
                suggestion=f"보관 기간을 {min_years}년 이상으로 고치십시오.",
                fixes=[{"doc": doc.key, "old": f.raw, "new": F.restyle("retention", f.raw, F.Fact("retention", str(min_years), f"{min_years}년")) or f.raw}],
                code="RETENTION_SHORT"))
            break
    if protocol is not None and doc.doc_type == "consent_form" and protocol.consent_plan == "waiver":
        out.append(_issue(
            "minor", "regulatory", "동의 면제로 계획된 과제에 동의서가 포함되어 있습니다", doc=doc,
            suggestion="과제 정보의 동의 계획(면제/취득)과 제출 서류가 일치하는지 확인하십시오.", code="CONSENT_PLAN_MISMATCH"))
    return out


# ------------------------------------------------------------------ 교차 대조
def cross_check(docs: list[QCDoc], protocol: Any | None) -> list[dict]:
    fields = criteria().get("cross_fields", {})
    record = {f.key: f for f in (F.record_facts(protocol) if protocol is not None else [])}
    out = []
    for key, meta in fields.items():
        per_doc = {d.key: [f for f in d.facts if f.key == key] for d in docs}
        per_doc = {k: v for k, v in per_doc.items() if v}
        if not per_doc:
            continue
        by_key = {d.key: d for d in docs}
        ref = record.get(key)
        if ref is not None:
            for dk, fs in per_doc.items():
                if any(f.value == ref.value for f in fs):
                    continue
                doc = by_key[dk]
                wrong = [fs[0]] if key == "subjects" else [f for f in fs if f.value != ref.value]
                fixes = [{"doc": dk, "old": f.raw, "new": new} for f in wrong
                         if (new := F.restyle(key, f.raw, ref)) and new != f.raw]
                out.append(_issue(
                    meta["severity"], "record", f"{meta['label']}이(가) 과제 정보와 다릅니다", doc=doc,
                    detail=f"{doc.label}: {_show(key, fs[0])} / 과제 정보: {_show(key, ref)}", evidence=fs[0].line,
                    suggestion=f"과제 정보({_show(key, ref)})와 일치시키거나, 과제 정보가 틀렸다면 과제를 먼저 수정하십시오.",
                    fixes=fixes, code=f"RECORD_{key.upper()}"))
            continue
        primaries = {dk: fs[0] for dk, fs in per_doc.items()}
        values = Counter(f.value for f in primaries.values())
        if len(values) < 2:
            continue
        proto_doc = next((d for d in docs if d.doc_type == "protocol" and d.key in primaries), None)
        ref = primaries[proto_doc.key] if proto_doc else next(
            f for f in primaries.values() if f.value == values.most_common(1)[0][0])
        ref_doc = proto_doc or next(by_key[k] for k, f in primaries.items() if f is ref)
        for dk, f in primaries.items():
            if f.value == ref.value:
                continue
            doc = by_key[dk]
            new = F.restyle(key, f.raw, ref)
            out.append(_issue(
                meta["severity"], "cross", f"{meta['label']} 불일치: {doc.label} ↔ {ref_doc.label}", doc=doc,
                detail=f"{doc.label}: {_show(key, f)} / {ref_doc.label}: {_show(key, ref)}", evidence=f.line,
                suggestion=f"{ref_doc.label} 기준({_show(key, ref)})으로 일치시키십시오. 기준 문서가 틀렸다면 기준 문서를 고치십시오.",
                fixes=[{"doc": dk, "old": f.raw, "new": new}] if new and new != f.raw else [],
                code=f"CROSS_{key.upper()}"))
    return out


def _show(key: str, f: F.Fact) -> str:
    if key in ("subjects",):
        return f"{int(f.value):,}명"
    if key in ("visits",):
        return f"{f.value}회"
    if key == "retention":
        return f"{f.value}년"
    if key in ("title",):
        return f.raw[:60]
    return f.raw


# ------------------------------------------------------------------ Phase 3
def check_typos(doc: QCDoc) -> list[dict]:
    out = []
    for wrong, right in (criteria().get("typos") or {}).items():
        if not right or wrong not in doc.text:
            continue
        out.append(_issue(
            "minor", "typo", f"오탈자: '{wrong}' → '{right}'", doc=doc, evidence=_context(doc.text, wrong),
            suggestion=f"'{right}'(으)로 고치십시오.", fixes=[{"doc": doc.key, "old": wrong, "new": right, "all": True}],
            phase=3, code="TYPO"))
    dup = re.search(r"(?<![가-힣A-Za-z])([가-힣]{2,})[ \t]+\1(?![가-힣A-Za-z])", doc.text)
    if dup:
        out.append(_issue(
            "minor", "typo", f"같은 단어 반복: '{dup.group(1)} {dup.group(1)}'", doc=doc, evidence=_context(doc.text, dup.group(0)),
            suggestion="중복된 단어를 지우십시오.", fixes=[{"doc": doc.key, "old": dup.group(0), "new": dup.group(1)}],
            phase=3, code="TYPO_DUP"))
    return out


def check_numbers(doc: QCDoc) -> list[dict]:
    out = []
    for bad in F.invalid_dates(doc.text)[:3]:
        out.append(_issue("major", "numbers", f"존재하지 않는 날짜: {bad}", doc=doc, evidence=_context(doc.text, bad),
                          suggestion="날짜를 확인하십시오.", phase=3, code="DATE_INVALID"))
    for a, b, raw, line in F.date_ranges(doc.text):
        if a and b and b < a:
            out.append(_issue("major", "numbers", "기간의 종료일이 시작일보다 앞섭니다", doc=doc, evidence=line,
                              detail=raw, suggestion="시작일과 종료일을 확인하십시오.", phase=3, code="RANGE_REVERSED"))
    grouped: dict[str, set[str]] = defaultdict(set)
    raws: dict[str, list[F.Fact]] = defaultdict(list)
    for f in doc.facts:
        if f.key in ("dosage", "visits", "duration", "study_period"):
            grouped[f.key].add(f.value)
            raws[f.key].append(f)
    labels = criteria().get("cross_fields", {})
    for key, vals in grouped.items():
        if len(vals) > 1:
            fs = raws[key]
            out.append(_issue(
                "major", "numbers", f"문서 안에서 {labels.get(key, {}).get('label', key)} 표기가 서로 다릅니다", doc=doc,
                detail=" / ".join(dict.fromkeys(_show(key, f) for f in fs)), evidence=fs[0].line,
                suggestion="같은 문서 안의 표기를 하나로 통일하십시오.", phase=3, code=f"SELF_{key.upper()}"))
    return out


# ------------------------------------------------------------------ 전체
def run_rules(docs: list[QCDoc], protocol: Any | None) -> tuple[list[dict], list[dict]]:
    """(phase1 이슈, phase3 이슈)"""
    conds = conditions(protocol)
    for d in docs:
        d.facts = F.extract(d.text)
    p1: list[dict] = []
    p3: list[dict] = []
    for d in docs:
        if not d.text.strip():
            p1.append(_issue("major", "required", "본문 텍스트를 읽을 수 없습니다", doc=d,
                             detail="; ".join(d.warnings) or "스캔본(이미지)이거나 지원하지 않는 형식입니다.",
                             suggestion="텍스트가 있는 PDF·DOCX·HWPX로 올리거나 AI 문서 인식을 켜십시오.", code="NO_TEXT"))
            continue
        p1 += check_required(d, conds) + check_version(d) + check_placeholders(d) + check_pii(d)
        p1 += check_regulatory(d, protocol)
        p3 += check_typos(d) + check_numbers(d)
    readable = [d for d in docs if d.text.strip()]
    p1 += check_terminology(readable) + cross_check(readable, protocol)
    return p1, p3
