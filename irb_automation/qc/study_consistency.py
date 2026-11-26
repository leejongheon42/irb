"""Find directly comparable research outcomes and explicitly listed CRF fields."""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, NamedTuple

from .consent_disclosures import sentences
from .rules import flag_meta

if TYPE_CHECKING:
    from .checks import QCDoc

_ROLE = r"[일이12]\s*차"
_ROLE_LABELS = {"일": "1차", "1": "1차", "이": "2차", "2": "2차"}
_ENDPOINT_KIND = r"(?:연구\s*)?목적|평가\s*(?:변수|지표)|(?:통계\s*)?분석"
_ENDPOINT_CLAUSE = re.compile(
    rf"(?P<role>{_ROLE})\s*(?P<kind>{_ENDPOINT_KIND})\s*(?:은|는|:|：)\s*"
    rf"(?P<value>.+?)(?=[,.]?\s*(?:이며|이고)?\s*\b{_ROLE}\s*(?:{_ENDPOINT_KIND})|$)", re.S,
)
_ANALYTE_TOKEN = r"(?<![A-Za-z0-9])(?:[A-Z](?:\r?\n[ \t]*)?)+[A-Za-z0-9]*(?:[-‐‑–][A-Za-z0-9α-ω]+)*"
_ANALYTE = re.compile(_ANALYTE_TOKEN)
_DEPENDENT_VARIABLE = re.compile(
    rf"(?P<marker>{_ANALYTE_TOKEN})\s*(?:의\s*)?(?:변화(?:량|율)?\s*)?"
    r"(?:을|를)\s*종속\s*변수",
)
_MEASUREMENT = re.compile(r"(?:정량|측정)\s*(?:한다|하였다|합니다|할\s*예정)")
_DECLARATION = re.compile(
    r"(?:임상\s*정보|수집\s*(?:하는\s*)?(?:개인\s*)?(?:정보|자료|항목)|(?:분석|연구)\s*자료)"
    r"\s*(?:에는|는|은|:|：)\s*([^.!?。]+?)(?:입니다|이다|을\s*수집|를\s*수집|[.!?。]|$)"
)
_CLINICAL_TABLE = re.compile(
    r"(?m)^\s*(?:#\s*)?(?:\d+[.)]\s*)?(?:임상\s*정보|임상\s*자료|수집\s*항목)[ \t]*$"
    r"(?P<rows>[\s\S]*?)(?=^\s*(?:#\s*|\d+[.)]\s+)\S|\Z)"
)
_TABLE_LABELS = {"항목", "구분", "값", "기록", "임상정보", "수집항목"}
_INCOME_LABEL = (
    r"(?<![가-힣])(?:(?P<holder>가족|가구|세대|본인|개인)\s*)?"
    r"(?:(?P<period>[연월])(?:간)?\s*)?소득(?!\s*(?:유무|여부|구간|분위|수준))"
)
_INCOME_TERMS = re.compile(_INCOME_LABEL)
_HOUSEHOLD_LABELS = {"가족", "가구", "세대"}
_CRF_INCOME_ROW = re.compile(
    rf"(?m)^[ \t]*(?P<label>{_INCOME_LABEL})"
    r"[ \t]*(?:[|:：]|\r?\n)[ \t]*(?:\d{1,3}(?:,\d{3})+|\d+)\s*(?:억\s*|만\s*)?원[ \t]*$"
)


class Citation(NamedTuple):
    doc: QCDoc
    quote: str

    def fields(self) -> dict[str, str]:
        return {"doc": self.doc.key, "name": self.doc.name, "quote": self.quote.strip(), "value": "", "norm": ""}


class EndpointClause(NamedTuple):
    role: str
    kind: str
    quote: str
    value: str


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
    clauses: list[EndpointClause] = []
    methods: list[tuple[str, set[str]]] = []
    for sentence in sentences(doc.text):
        matches = list(_ENDPOINT_CLAUSE.finditer(sentence))
        clauses.extend(EndpointClause(_ROLE_LABELS[m["role"][0]], m["kind"], m[0].strip(), m["value"]) for m in matches)
        if not matches and _MEASUREMENT.search(sentence):
            methods.append((sentence.strip(), _markers(sentence)))
    for role in ("1차", "2차"):
        objectives = [clause for clause in clauses if clause.role == role and "목적" in clause.kind]
        endpoints = [clause for clause in clauses if clause.role == role and "평가" in clause.kind]
        if len(objectives) != 1 or len(endpoints) != 1:
            continue
        objective, outcome = objectives[0], endpoints[0]
        goal, endpoint = _markers(objective.value), _markers(outcome.value)
        if len(endpoint) != 1 or not goal or goal & endpoint:
            continue
        analyses = [(clause.quote, _markers(match["marker"]))
                    for clause in clauses if clause.role == role and "분석" in clause.kind
                    for match in _DEPENDENT_VARIABLE.finditer(clause.value)]
        if analyses:
            # The dependent variable identifies the analyzed outcome. Covariates and other measurements can differ.
            if any(not markers or not markers <= goal for _, markers in analyses):
                continue
            method_quote = analyses[0][0]
        else:
            method_quote = next((quote for quote, markers in methods if markers & goal), "")
            if not method_quote or any(markers & endpoint for _, markers in methods):
                continue
        yield _candidate(
            doc, "SEMANTIC_ENDPOINT", f"{role} 연구 목적과 평가변수 불일치 확인",
            f"평가변수에는 {next(iter(endpoint))}가 기재돼 있습니다. 목적 및 분석·측정 방법의 지표와 대조하십시오.",
            "같은 우선순위의 연구 목적·분석 또는 측정 방법·평가변수를 대조하고 서로 다른 지표를 사용한 이유를 확인하십시오.",
            "M10", "서술 모순", [Citation(doc, quote) for quote in (outcome.quote, objective.quote, method_quote)],
        )


def _field_label(text: str) -> str:
    return re.sub(r"\s+|\([^)]*\)|(?:등|으로\s*한정(?:한다|합니다)?)\s*$", "", text).casefold()


def _declared_fields(doc: QCDoc) -> list[Citation]:
    return [Citation(doc, m[0]) for m in _DECLARATION.finditer(doc.text) if re.search(r"[,，·]", m[1])]


def _crf_findings(doc: QCDoc, declarations: list[Citation]) -> Iterator[dict]:
    declared = {_field_label(c.quote) for c in declarations}
    income_disclosures = [term for citation in declarations for term in _INCOME_TERMS.finditer(citation.quote)]
    recorded = []
    for row in _CRF_INCOME_ROW.finditer(doc.text):
        if any((not term["holder"] or not row["holder"]
                or (term["holder"] in _HOUSEHOLD_LABELS) == (row["holder"] in _HOUSEHOLD_LABELS))
               and (not term["period"] or not row["period"] or term["period"] == row["period"])
               for term in income_disclosures):
            continue
        recorded.append((row["label"], row[0].strip()))
    for section in _CLINICAL_TABLE.finditer(doc.text):
        for line in section["rows"].splitlines():
            label, separator, value = line.partition("|")
            field = _field_label(label)
            # A narrow candidate when a named field is recorded in the clinical-data table.
            if (not separator or not value.strip() or field in _TABLE_LABELS
                    or not re.fullmatch(r"[가-힣]{2,}(?:상태|여부|습관|이력|횟수|유무)", field)
                    or any(field in listing or field[:-2] in listing for listing in declared)):
                continue
            recorded.append((label.strip(), line))
    for label, quote in recorded:
        yield _candidate(
            doc, "SEMANTIC_CRF_FIELDS", f"수집 목록에 없는 증례기록서 항목 확인: {label}",
            f"증례기록서에 '{label}' 항목이 기록돼 있습니다.",
            "계획서와 참여 설명문의 수집 범위에 포함하는지 검토하고 수집 항목 표기를 일치시키십시오.",
            "E06", "개인정보·이용·제공", [Citation(doc, quote), *declarations],
        )


def study_findings(docs: list[QCDoc]) -> Iterator[dict]:
    from .interview_amendment import interview_findings
    from .retrospective import retrospective_findings

    protocols = [doc for doc in docs if doc.doc_type == "protocol"]
    for doc in protocols:
        yield from _endpoint_findings(doc)
    if len(protocols) != 1:
        return
    yield from retrospective_findings(protocols[0], docs)
    yield from interview_findings(protocols[0], docs)
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


def combine_study_findings(docs: list[QCDoc], findings: list[dict]) -> list[dict]:
    """Prefer the cross-document waiver explanation to its generic missing-content duplicate."""
    specific = list(study_findings(docs))
    explained_waivers = {issue["doc"] for issue in specific if issue["code"] == "RETRO_WAIVER_RATIONALE"}
    return [*(issue for issue in findings if not (
        issue["doc"] in explained_waivers and issue["code"] == "REQ_WAIVER_REQUEST_IMPRACTICABLE"
    )), *specific]
