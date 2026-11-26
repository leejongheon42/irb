"""Public IRB submission requirements shared by prereview and QC."""
from functools import lru_cache
from pathlib import Path
from typing import TypedDict

import yaml

from ..engine.facts import ProtocolFacts
from .context import STUDY_DEFAULTS

CATALOG = Path(__file__).parents[1] / "qc" / "required_docs.yaml"


class DocumentRule(TypedDict, total=False):
    doc: str
    label: str
    level: str
    when: list[str]
    channel: str
    pattern: str
    basis: str
    form: dict[str, str]
    owner: str
    verified: bool
    forms: list[str]
    source_keys: list[list[str]]


@lru_cache(maxsize=4)
def _load_catalog(content: str) -> dict:
    return yaml.safe_load(content)


def catalog(content: str | None = None) -> list[DocumentRule]:
    return _pack(content)["documents"]


def report_catalog(kind: str, content: str | None = None) -> list[DocumentRule]:
    """Return the requirements specific to a submission report."""
    return _pack(content).get("reports", {}).get(kind, [])


def _pack(content: str | None) -> dict:
    return _load_catalog(content if content is not None else CATALOG.read_text(encoding="utf-8"))


REPORTS = {
    "amendment": ("amendment_table", "변경대비표"),
    "continuing": ("progress_report", "지속심의 보고"),
    "final_report": ("final_report", "종료·결과보고"),
    "early_termination": ("early_termination", "조기종료 보고"),
    "deviation": ("deviation_report", "연구계획 위반·이탈 보고"),
    "adverse_event": ("adverse_event", "이상반응 보고"),
    "coi_update": ("coi", "이해상충 변경 보고"),
    "info_provision": ("info_provision", "개인정보 제공 신청"),
    "specimen_provision": ("specimen_provision", "인체유래물 제공 신청"),
    "disposal_transfer": ("disposal_transfer", "인체유래물 폐기·이관 신청"),
}


def research_classification(facts: ProtocolFacts) -> tuple[str, list[str]]:
    category, methods = STUDY_DEFAULTS.get(facts.study_type, ("", []))
    return facts.research_category or category, facts.research_method or methods


def conditions(facts: ProtocolFacts) -> set[str]:
    category, methods = research_classification(facts)
    conditions = {category, facts.application_route, facts.access_route, *methods, *facts.consent_routes}
    if category == "both":
        conditions.update(("human", "specimen"))
    if not facts.existing_data_only:
        conditions.update(("collect", "direct_collection"))
    if facts.external_transfer:
        conditions.add("external")
    if facts.proxy_consent:
        conditions.add("proxy")
    if facts.multicenter:
        conditions.add("multicenter")
    if facts.subject_age_min is not None and facts.subject_age_max is not None and facts.subject_age_min <= 12 and facts.subject_age_max >= 7:
        conditions.add("children")
    return conditions


def applicable(item: DocumentRule, conditions: set[str]) -> bool:
    return set(item.get("when", [])) <= conditions


def compute(facts: ProtocolFacts, content: str | None = None) -> list[dict]:
    conds = conditions(facts)
    kind = facts.submission_kind
    reports = _pack(content).get("reports", {})
    if kind in reports:
        entries: list[DocumentRule] = list(reports[kind])
        if kind == "amendment":
            entries.extend(item for item in catalog(content) if item["doc"] in facts.changed_doc_types
                           and applicable(item, conds))
    else:
        entries = [item for item in catalog(content) if applicable(item, conds)]
    result = []
    for item in entries:
        key = item["doc"]
        level = item["level"]
        channel = item.get("channel") or ("external_eirb" if level == "eirb_input" else "file")
        answer = facts.requirement_states.get(key, "")
        supplied = key in facts.uploaded_doc_types and channel != "external_eirb"
        satisfied = supplied or (answer == "declared_submitted" and channel in ("external_eirb", "eirb_or_file")) or answer == "not_applicable"
        result.append({**item, "required": level == "required", "provided": satisfied, "channel": channel,
                       "owner": item.get("owner", "researcher"), "verified": item.get("verified", False),
                       "condition": " · ".join(item.get("when", [])) or "공통",
                       "status": "provided" if supplied else answer or "missing",
                       "forms": item.get("forms", []) + [form for condition, form in item.get("form", {}).items() if condition in conds]})
    return result


def source_keys(item: dict) -> set[tuple[str, str]]:
    keys = {tuple(key) for key in item.get("source_keys", [])}
    for label in item.get("forms", []):
        if label.startswith("권고 제") and label.endswith("호"):
            keys.add(("권고서식", label.removeprefix("권고 제").removesuffix("호")))
    return keys
