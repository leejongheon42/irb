"""Find directly comparable research outcomes and explicitly listed CRF fields."""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, NamedTuple

from .consent_disclosures import sentences
from .rules import flag_meta

if TYPE_CHECKING:
    from .checks import QCDoc

_ROLE = r"일차|이차|1차|2차"
_ENDPOINT_CLAUSE = re.compile(
    rf"(?P<role>{_ROLE})\s*(?P<kind>(?:연구\s*)?목적|평가\s*변수|평가\s*지표)\s*(?:은|는|:|：)\s*"
    rf"(?P<value>.+?)(?=(?:[,.]?\s*(?:이며|이고)?\s*)\b(?:{_ROLE})\s*(?:평가|목적|연구)|$)", re.S,
)
_ANALYTE = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z](?:\r?\n[ \t]*)?){1,}[A-Za-z0-9]*(?:[-‐‑–][A-Za-z0-9α-ω]+)*")
_MEASUREMENT = re.compile(r"(?:정량|측정)\s*(?:한다|하였다|합니다|할\s*예정)")
_DECLARATION = re.compile(
    r"(?:임상\s*정보|수집\s*(?:정보|자료|항목))\s*(?:는|은|:|：)\s*([^.!?]+?)(?:입니다|이다|을\s*수집|를\s*수집|[.!?]|$)"
)
_CLINICAL_TABLE = re.compile(
    r"(?m)^\s*(?:#\s*)?(?:\d+[.)]\s*)?(?:임상\s*정보|임상\s*자료|수집\s*항목)[ \t]*$"
    r"(?P<rows>[\s\S]*?)(?=^\s*(?:#\s*|\d+[.)]\s+)\S|\Z)"
)
_TABLE_LABELS = {"항목", "구분", "값", "기록", "임상정보", "수집항목"}


class Citation(NamedTuple):
    doc: QCDoc
    quote: str

    def fields(self) -> dict[str, str]:
        return {"doc": self.doc.key, "name": self.doc.name, "quote": self.quote.strip(), "value": "", "norm": ""}


def _markers(text: str) -> set[str]:
    return {marker for match in _ANALYTE.finditer(text)
            if len(marker := "".join(match[0].split())) > 1}


def _candidate(doc: QCDoc, code: str, title: str, detail: str, suggestion: str,
               rule: str, flag: str, citations: list[Citation]) -> dict:
    from .checks import _issue

    cross = len({c.doc.key for c in citations}) > 1
    issue = _issue(flag_meta(rule, flag).get("severity") or "major", "cross" if cross else "context",
                   title, doc=doc, evidence=citations[0].quote, detail=detail, suggestion=suggestion, code=code)
    return dict(issue, rule_id=rule, flags=[flag], locations=[c.fields() for c in citations],
                result="needs_check", reason="review_pending", original_result="mismatch", question=suggestion)


def _endpoint_findings(doc: QCDoc) -> Iterator[dict]:
    clauses = [(m["role"].replace("일", "1").replace("이", "2"), "목적" in m["kind"], m[0].strip(), _markers(m["value"]))
               for sentence in sentences(doc.text) for m in _ENDPOINT_CLAUSE.finditer(sentence)]
    methods = [(sentence.strip(), _markers(sentence)) for sentence in sentences(doc.text)
               if _MEASUREMENT.search(sentence) and not _ENDPOINT_CLAUSE.search(sentence)]
    for role in ("1차", "2차"):
        objectives = [(quote, markers) for priority, objective, quote, markers in clauses if priority == role and objective]
        endpoints = [(quote, markers) for priority, objective, quote, markers in clauses if priority == role and not objective]
        if len(objectives) != 1 or len(endpoints) != 1:
            continue
        (goal_quote, goal), (endpoint_quote, endpoint) = objectives[0], endpoints[0]
        method_quote = next((quote for quote, markers in methods if markers & goal), "")
        if len(endpoint) != 1 or not goal or goal & endpoint or not method_quote or any(markers & endpoint for _, markers in methods):
            continue
        yield _candidate(
            doc, "SEMANTIC_ENDPOINT", f"{role} 연구 목적과 평가변수 불일치 확인",
            f"평가변수에는 {next(iter(endpoint))}가 기재돼 있습니다. 목적 및 측정 방법의 지표와 대조하십시오.",
            "같은 우선순위의 연구 목적·측정 방법·평가변수를 대조하고 서로 다른 지표를 사용한 이유를 확인하십시오.",
            "M10", "서술 모순", [Citation(doc, quote) for quote in (goal_quote, method_quote, endpoint_quote)],
        )


def _field_label(text: str) -> str:
    return re.sub(r"\s+|\([^)]*\)|(?:등|으로\s*한정(?:한다|합니다)?)\s*$", "", text).casefold()


def _declared_fields(doc: QCDoc) -> list[Citation]:
    return [Citation(doc, m[0]) for m in _DECLARATION.finditer(doc.text) if re.search(r"[,，·]", m[1])]


def _crf_findings(doc: QCDoc, declarations: list[Citation]) -> Iterator[dict]:
    declared = {_field_label(c.quote) for c in declarations}
    for section in _CLINICAL_TABLE.finditer(doc.text):
        for line in section["rows"].splitlines():
            label, separator, value = line.partition("|")
            field = _field_label(label)
            # A narrow candidate when a named field is recorded in the clinical-data table.
            if (not separator or not value.strip() or field in _TABLE_LABELS
                    or not re.fullmatch(r"[가-힣]{2,}(?:상태|여부|습관|이력|횟수|유무)", field)
                    or any(field in listing or field[:-2] in listing for listing in declared)):
                continue
            yield _candidate(
                doc, "SEMANTIC_CRF_FIELDS", f"수집 목록에 없는 증례기록서 항목 확인: {label.strip()}",
                f"증례기록서 임상정보 표에 '{label.strip()}' 항목이 기록돼 있습니다.",
                "계획서와 참여 설명문의 수집 범위에 포함하는지 검토하고 수집 항목 표기를 일치시키십시오.",
                "E06", "개인정보·이용·제공", [Citation(doc, line), *declarations],
            )


def study_findings(docs: list[QCDoc]) -> Iterator[dict]:
    protocols = [doc for doc in docs if doc.doc_type == "protocol"]
    for doc in protocols:
        yield from _endpoint_findings(doc)
    if len(protocols) != 1:
        return
    declarations = _declared_fields(protocols[0])
    if not declarations:
        return
    for doc in docs:
        if doc.doc_type == "consent_form":
            disclosures = _declared_fields(doc)
            if not disclosures:
                return
            declarations.extend(disclosures)
    for doc in docs:
        if doc.doc_type == "crf":
            yield from _crf_findings(doc, declarations)
