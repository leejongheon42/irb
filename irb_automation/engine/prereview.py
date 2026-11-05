"""자동 사전검토 오케스트레이터."""
from __future__ import annotations

from typing import Any

from ..timeutil import now
from .checklist import build_checklist
from .classifier import classify
from .consent import assess_consent
from .facts import ProtocolFacts
from .findings import generate_findings
from .risk import assess_risk
from .ruleset import Ruleset, get_ruleset

ENGINE_VERSION = "1.0.0"

REVIEW_TYPE_LABELS = {"exempt": "심의면제", "expedited": "신속심의", "full": "정규심의"}


def run_prereview(facts: ProtocolFacts, rs: Ruleset | None = None) -> dict[str, Any]:
    rs = rs or get_ruleset()
    risk = assess_risk(facts, rs)
    classification = classify(facts, risk, rs)
    review_type = classification["review_type"]
    consent = assess_consent(facts, risk, rs)
    checklist = build_checklist(facts, review_type, rs)
    findings = generate_findings(facts, risk, classification, consent, checklist, rs)

    summary = {sev: 0 for sev in ("critical", "major", "minor", "info")}
    for f in findings:
        summary[f["severity"]] += 1

    ready = summary["critical"] == 0
    next_steps: list[str] = []
    if not ready:
        next_steps.append("필수 보완 사항을 해결한 뒤 제출하십시오." if facts.submission_kind == "initial"
                          else "필수 보완 사항이 있어 IRB 간사가 보완을 요청할 수 있습니다.")
    if review_type == "exempt":
        next_steps.append("IRB 간사/위원장의 심의면제 확인 절차로 진행됩니다.")
    elif review_type == "expedited":
        next_steps.append("심의위원 1~2인의 신속심의로 진행됩니다.")
    else:
        next_steps.append("정기 위원회 회의(정규심의) 안건으로 상정됩니다.")

    return {
        "engine_version": ENGINE_VERSION,
        "ruleset_version": rs.version,
        "generated_at": now().isoformat(),
        "submission_kind": facts.submission_kind,
        "review_type": review_type,
        "review_type_label": REVIEW_TYPE_LABELS[review_type],
        "risk": risk.to_dict(),
        "classification": classification,
        "consent": consent,
        "checklist": checklist,
        "findings": findings,
        "summary": summary,
        "ready_for_review": ready,
        "next_steps": next_steps,
        "disclaimer": "본 결과는 규칙 기반 자동 사전검토 권고이며, 최종 심의 유형과 승인 여부는 IRB가 결정합니다.",
    }
