"""Submission bundle checks (E10)."""
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from ..public_irb.requirements import applicable, compute, report_catalog
from ..public_irb.requirements import catalog as public_catalog
from .profile import Profile, profile_conditions

if TYPE_CHECKING:
    from ..models import Protocol
    from .checks import QCDoc


def inspect_bundle(docs: list["QCDoc"], profile: Profile, protocol: "Protocol | None" = None) -> tuple[list, list]:
    from .checks import _issue

    checks, issues = [], []
    roles = defaultdict(list)
    for doc in docs:
        roles[doc.doc_type].append(doc)
    conds = profile_conditions(profile)
    body = "\n".join(d.text for d in roles.get("protocol", []))
    linked_public = protocol is not None and protocol.committee == "public_irb"
    if profile.get("committee") == "institution" and not linked_public:
        from ..engine.checklist import build_checklist
        from ..engine.facts import ProtocolFacts
        from ..engine.ruleset import get_ruleset

        if protocol is None:
            catalog = []
        else:
            facts = ProtocolFacts.from_protocol(protocol)
            review_type = protocol.review_type.value if protocol.review_type else "full"
            catalog = [{"doc": e["doc"], "label": e["label"], "level": "required"}
                       for e in build_checklist(facts, review_type, get_ruleset())]
    elif protocol is not None and protocol.committee == "public_irb":
        from sqlalchemy.orm import object_session

        from ..engine.facts import ProtocolFacts
        from ..workflow import service as wf

        db = object_session(protocol)
        facts = (wf.build_facts(db, protocol, wf.preview_kind(protocol)) if db is not None
                 else ProtocolFacts.from_protocol(protocol))
        catalog = compute(facts)
    elif profile.get("application") == "amendment":
        catalog = list(report_catalog("amendment"))
        if re.search(r"(?:면담\s*전|아직\s*면담)[^.\n]{0,70}추가\s*선택서", body):
            catalog.append({"doc": "recording_choice", "label": "기존 참여자용 녹음 추가설명·선택서", "level": "required"})
    else:
        catalog = public_catalog()
        if {"new", "records", "existing_internal"} <= conds:
            catalog = [*catalog, {"doc": "records_access_evidence", "label": "의무기록 이용 허가·확인 증빙 (기관 요구 시)",
                                  "level": "conditional", "pattern": r"(?:기관|관리부서)\s*(?:자료\s*)?이용\s*(?:허가|승인)"}]
    for item in catalog:
        role, label = item["doc"], item["label"]
        present = roles.get(role, [])
        answer = profile.get("bundle_answers", {}).get(role)
        condition_met = linked_public or applicable(item, conds)
        level = item["level"]
        result, reason = "match", ""
        if not present and item.get("status") in ("declared_submitted", "not_applicable"):
            result, reason = "na", "과제의 제출 상태를 반영했습니다."
        elif not present and answer in ("out_of_scope", "not_applicable"):
            result, reason = "na", "이번 점검 범위 아님" if answer == "out_of_scope" else "제출자가 해당 없음으로 표시했습니다."
        elif not condition_met:
            if role in ("consent_form", "waiver_request") and not profile.get("consent"):
                result, reason = "needs_check", "applicability_unknown"
            elif role in ("training_cert", "exemption_request") and profile.get("application") == "unknown":
                result, reason = "needs_check", "applicability_unknown"
            elif role in ("crf", "questionnaire") and not profile.get("method"):
                result, reason = "needs_check", "applicability_unknown"
            else:
                result, reason = "na", "신청 경로의 적용 조건에 해당하지 않습니다."
        elif role == "coi" and level == "eirb_input" and not present:
            result, reason = "needs_check", "submission_unverified"
        elif level in ("evidence", "eirb_input") and not present:
            result, reason = "na", "관련 증빙 또는 e-IRB 입력을 확인하십시오."
        elif not present and answer == "eirb" and item.get("channel") == "eirb_or_file":
            result, reason = "na", "제출자가 해당 없음 또는 e-IRB 입력으로 표시했습니다."
        elif present and not any(d.text.strip() for d in present):
            result, reason = "needs_check", "unreadable"
        elif not present:
            if level == "conditional" and not re.search(item.get("pattern", r"(?!)"), body):
                result, reason = "na", "계획서에서 해당 자료의 사용 근거를 찾지 못했습니다."
            else:
                result, reason = "needs_check", "file_missing"
        checks.append({"rule": "E10", "field": role, "label": label, "scope": "제출 묶음",
                       "docs": [d.key for d in present], "result": result, "reason": reason,
                       "channel": item.get("channel", ""),
                       "forms": item.get("forms") or [form for cond, form in item.get("form", {}).items() if cond in conds]})
        if result == "needs_check" and reason != "unreadable":
            question = (f"{label}의 적용 여부와 제출 방식(파일 또는 해당하는 경우 e-IRB)을 확인해 주세요."
                        if role != "coi" else "연구자별 이해상충공개가 e-IRB에 입력됐는지 확인해 주세요.")
            issues.append({**_issue("major", "required", f"{label} 제출 여부 확인", code="BUNDLE_REQUIRED"),
                           "rule_id": "E10", "flags": ["제출 묶음 구성"], "result": result,
                           "reason": reason, "field": role, "basis": item.get("basis", ""),
                           "bundle_answer": answer if answer == "later" else "",
                           "question": question})
    for role, same_role in roles.items():
        if role != "other" and len(same_role) == 1:
            continue
        question = "이 파일들의 문서 종류를 확인해 주세요." if role == "other" else "파일별 대상이나 기관과 제출할 파일을 확인해 주세요."
        issues.append({**_issue("major", "required", "제출 파일의 역할 확인", code="BUNDLE_ROLE",
                                detail=" / ".join(d.name for d in same_role)),
                       "rule_id": "E10", "flags": ["제출 묶음 구성"], "result": "needs_check",
                       "reason": "scope_unclear", "field": f"{role}_role",
                       "docs": [d.key for d in same_role], "question": question})
        if role != "other" and len(same_role) > 1:
            from .facts import version_from_filename

            versions = {version_from_filename(d.name) for d in same_role}
            if None not in versions and len(versions) == 1 and len({d.text for d in same_role if d.text.strip()}) > 1:
                issues[-1]["flags"].append("버전 의미 충돌")
                issues[-1]["related_rules"] = ["M01"]
    return checks, issues


def mixed_study_findings(docs: list["QCDoc"]) -> list[dict]:
    """Flag three independently conflicting identifiers against a unique protocol."""
    from .checks import _issue

    anchors = [doc for doc in docs if doc.doc_type == "protocol"]
    if len(anchors) != 1:
        return []
    anchor = anchors[0]
    fields = ("title", "study_no", "pi")
    base = {key: {f.value for f in anchor.facts if f.key == key} for key in fields}
    if any(len(values) != 1 for values in base.values()):
        return []
    out = []
    for doc in docs:
        if doc is anchor:
            continue
        values = {key: {f.value for f in doc.facts if f.key == key} for key in fields}
        if all(len(values[key]) == 1 and values[key].isdisjoint(base[key]) for key in fields):
            out.append({**_issue("major", "required", "다른 연구의 문서가 포함됐는지 확인해 주세요.", doc=doc,
                                 code="BUNDLE_MIXED_STUDY", detail=f"{anchor.name}와 제목·연구번호·책임연구자가 모두 다릅니다."),
                        "rule_id": "E10", "flags": ["제출 묶음 구성"], "field": "study_identity",
                        "docs": [anchor.key, doc.key]})
    return out
