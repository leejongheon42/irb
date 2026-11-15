"""Derive application conditions and keep the source of each decision."""
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    from ..models import Protocol
    from .checks import QCDoc

Source = Literal["default", "doc", "record", "user"]


class Answers(TypedDict, total=False):
    compare_only: bool
    committee: str
    study_type: str
    application: str
    data_source: list[str]
    consent: list[str]
    method: list[str]
    drug: bool
    visits: bool
    vulnerable: list[str]
    multicenter: bool
    external_transfer: bool
    student_pi: bool
    bundle_answers: dict[str, str]


class Profile(Answers):
    source: dict[str, Source]
    evidence: dict[str, str]


def build_profile(answers: Answers | None, protocol: "Protocol | None", docs: Iterable["QCDoc"]) -> Profile:
    documents = list(docs)
    roles = {d.doc_type for d in documents}
    body = "\n".join(d.text for d in documents)
    result = Profile(committee="institution" if protocol else "public_irb", study_type="unknown",
                     application="unknown", source=dict.fromkeys(("committee", "study_type", "application"), "default"),
                     evidence={})
    if "exemption_request" in roles:
        result.update(application="exemption")
        result["source"]["application"] = "doc"
    consent = [value for role, value in (("consent_form", "obtain"), ("waiver_request", "waiver_request"))
               if role in roles]
    if consent:
        result.update(consent=consent)
        result["source"]["consent"] = "doc"
    sentences = re.split(r"\n|[.!?](?:\s|$)", body)
    relevant = [s for s in sentences if not re.search(r"제외|미포함|포함하지|해당하지 않", s)]
    vulnerable = []
    for value, pattern in (("children", r"만\s*\d+세\s*미만|아동"), ("proxy", r"법정대리인")):
        evidence = next((s for s in relevant if re.search(pattern, s)), "")
        if evidence:
            vulnerable.append(value)
            result["evidence"].setdefault("vulnerable", evidence.strip()[:200])
    if vulnerable:
        result.update(vulnerable=vulnerable)
        result["source"]["vulnerable"] = "doc"
    from .facts import extract

    facts = extract(body)
    keys = {fact.key for fact in facts}
    if "dosage" in keys or any(f.key == "drug" for f in facts):
        result.update(drug=True)
        result["source"]["drug"] = "doc"
        result["evidence"]["drug"] = next(f.line for f in facts if f.key in ("dosage", "drug"))
    if "visits" in keys:
        result.update(visits=True)
        result["source"]["visits"] = "doc"
        result["evidence"]["visits"] = next(f.line for f in facts if f.key == "visits")
    if protocol:
        result["committee"] = protocol.committee or "institution"
        result["source"]["committee"] = "record"
        result.update(consent=["waiver_request" if protocol.consent_plan == "waiver" else "obtain"],
                      data_source=["existing_internal" if protocol.existing_data_only else "collect"],
                      vulnerable=list(protocol.vulnerable_populations or []),
                      multicenter=protocol.multicenter, external_transfer=protocol.external_transfer)
        result["source"].update(dict.fromkeys(("consent", "data_source", "vulnerable", "multicenter",
                                               "external_transfer"), "record"))
        if protocol.review_type:
            result.update(application="exemption" if protocol.review_type.value == "exempt" else "new")
            result["source"]["application"] = "record"
        if result["committee"] == "public_irb":
            from ..engine.facts import ProtocolFacts
            from ..public_irb.requirements import conditions, research_classification

            record = ProtocolFacts.from_protocol(protocol)
            category, methods = research_classification(record)
            vulnerable = sorted(conditions(record) & {"children", "proxy"})
            result.update(study_type=category or "unknown", method=methods, application=record.application_route,
                          consent=list(record.consent_routes), vulnerable=vulnerable)
            result["source"].update(dict.fromkeys(("study_type", "method", "application", "consent", "vulnerable"), "record"))
    if answers:
        result.update(answers)
        result["source"].update(dict.fromkeys(answers, "user"))
    return result


def profile_conditions(profile: Profile) -> set[str]:
    conds = {v for v in (profile.get("study_type"), profile.get("application")) if v and v != "unknown"}
    for key in ("data_source", "consent", "method", "vulnerable"):
        conds.update(profile.get(key, []))
    conds.update(key for key in ("drug", "visits", "multicenter", "external_transfer", "student_pi") if profile.get(key))
    if "both" in conds:
        conds.update(("human", "specimen"))
    if conds & {"existing_internal", "external", "public", "bank"}:
        conds.add("existing")
    if "waiver_request" in conds:
        conds.add("waiver")
    if "collect" in conds:
        conds.add("prospective")
    if "collect" in profile.get("data_source", []):
        conds.add("direct_collection")
    return conds
