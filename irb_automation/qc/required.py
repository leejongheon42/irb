"""Content-based required-item checks with institution-configurable criteria."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, TypedDict

from .structure import Section, matches, split_sections, substantive

if TYPE_CHECKING:
    from .checks import QCDoc

_DENIED_WITHDRAWAL = r"re:철회(?:는|할\s*수)?\s*(?:불가능|불가|없습니다|없다|금지)"

class RequiredIssue(TypedDict):
    severity: str
    category: str
    title: str
    detail: str
    doc: str
    doc_name: str
    evidence: str
    suggestion: str
    fixes: list[dict[str, str]]
    phase: int
    source: str
    code: str
    status: str
    confidence: str
    basis: str
    section: str
    flags: list[str]


def check_required(doc: QCDoc, conds: set[str], units: list[dict] | None = None) -> list[RequiredIssue]:
    """Report absent or incomplete content, with its candidate section."""
    from .checks import criteria

    out: list[RequiredIssue] = []
    sections = split_sections(doc.text)
    rules = criteria()
    for item in rules.get("required", {}).get(doc.doc_type, []):
        when = set(item.get("when") or [])
        code = f"REQ_{doc.doc_type}_{item['id']}".upper()
        if when and not (when & conds):
            if units is not None:
                units.append({"rule": "M06", "field": code, "label": item["label"], "scope": doc.name,
                              "docs": [doc.key], "result": "na", "reason": "적용 조건에 해당하지 않습니다."})
            continue
        patterns = item.get("any", []) + item.get("all", [])
        candidates = [s for s in sections if any(matches(s.title + "\n" + s.body, p) for p in patterns)]
        if item.get("structured") and candidates:
            candidates = [Section("", "\n".join(s.title + "\n" + s.body for s in candidates),
                                  candidates[0].start, candidates[-1].end)]
        valid = False
        for section in candidates:
            content = section.title + "\n" + section.body
            all_ok = all(matches(content, p) for p in item.get("all", []))
            any_ok = not item.get("any") or any(matches(content, p) for p in item["any"])
            must_ok = all(matches(content, p) for p in item.get("must", []))
            forbidden = any(matches(content, p) for p in item.get("reject", []))
            enough = substantive(section.body, int(item.get("min_chars", rules.get("required_min_chars", 30))))
            if item.get("structured"):
                enough = len(content.strip()) >= int(item.get("min_chars", 8))
            if item.get("quantified"):
                # 수치로 답하는 항목("총 1,500명 … 검정력 85%")은 서술형 종결이 없어도 된다.
                enough = len(re.sub(r"\s", "", section.body)) >= int(item.get("min_chars", 15))
            if all_ok and any_ok and must_ok and enough and not forbidden:
                valid = True
                break
        denied = item["id"] == "voluntary" and any(
            matches(s.title + "\n" + s.body, _DENIED_WITHDRAWAL) for s in sections
            if "익명화" not in s.body and "자료 삭제" not in s.body)
        if denied:
            valid = False
        if valid:
            if units is not None:
                units.append({"rule": "M06", "field": code, "label": item["label"], "scope": doc.name,
                              "docs": [doc.key], "result": "match", "reason": ""})
            continue
        # 근거로 보여 줄 섹션: 제목이 항목과 맞는 것을 우선한다(본문에 단어만 스친 섹션 제외).
        titled = [s for s in candidates if any(matches(s.title, p) for p in patterns)]
        candidate = (titled or candidates)[0] if candidates else None
        label = item["label"]
        flags = []
        if item["id"] == "voluntary":
            flags = ["권리 설명 모순" if denied else "철회 권리 누락"]
        out.append({
            "severity": item.get("severity", "major"), "category": "required",
            "title": f"필수 기재사항 {'내용 미흡' if candidates else '누락'}: {label}",
            "detail": f"{doc.label}의 '{label}' 내용을 확인하십시오.",
            "doc": doc.key, "doc_name": doc.name,
            "evidence": (candidate.title + "\n" + candidate.body).strip()[:300] if candidate else "",
            "suggestion": item.get("rubric") or f"'{label}' 항목에 구체적인 내용을 작성하십시오.",
            "fixes": [], "phase": 1, "source": "rule",
            "code": code,
            "status": "partial" if candidates else "missing", "confidence": "rule",
            "basis": item.get("basis", ""), "section": candidate.title if candidate else "", "flags": flags,
        })
    return out
