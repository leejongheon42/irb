"""서면동의 면제 요건 평가 (생명윤리법 제16조제3항).

다음 두 요건을 "모두" 갖춘 경우 IRB 승인을 받아 서면동의를 면제할 수 있다.
 1. 연구대상자의 동의를 받는 것이 연구 진행과정에서 현실적으로 불가능하거나
    연구의 타당성에 심각한 영향을 미친다고 판단되는 경우
 2. 연구대상자의 동의 거부를 추정할 만한 사유가 없고, 동의를 면제하여도
    연구대상자에게 미치는 위험이 극히 낮은 경우
"""
from __future__ import annotations

from typing import Any

from .facts import ProtocolFacts
from .risk import RiskResult
from .ruleset import Ruleset

MIN_JUSTIFICATION_CHARS = 30


def assess_consent(facts: ProtocolFacts, risk: RiskResult, rs: Ruleset) -> dict[str, Any]:
    legal = rs.legal("bioethics_16_3")
    if facts.consent_plan == "obtain":
        return {
            "plan": "obtain",
            "status": "not_applicable",
            "status_label": "동의 취득 예정",
            "conditions": [],
            "notes": ["연구대상자 설명문 및 동의서 제출 필요"],
            "legal": legal,
        }

    reasons = [r for r in facts.waiver_impracticable_reasons if r]
    detail_ok = len(facts.waiver_impracticable_detail.strip()) >= MIN_JUSTIFICATION_CHARS
    cond1_met = bool(reasons) and detail_ok
    cond1_detail = ", ".join(rs.label("waiver_reasons", r) for r in reasons) or "사유 미선택"
    if not detail_ok:
        cond1_detail += f" / 구체적 설명 부족(최소 {MIN_JUSTIFICATION_CHARS}자)"

    low_risk = risk.level == "low" and not facts.is_identifiable
    sens_ok = not (set(facts.sensitive_categories) & {"genetic", "hiv_std"}) or facts.is_anonymized
    cond2_met = facts.waiver_no_presumed_refusal and low_risk and sens_ok
    parts = []
    parts.append("동의 거부 추정 사유 없음 확인" if facts.waiver_no_presumed_refusal else "동의 거부 추정 사유 검토 미기재")
    parts.append(f"위험도 {risk.level_label}({risk.score}점)")
    if facts.is_identifiable:
        parts.append("식별가능 정보 이용")
    if not sens_ok:
        parts.append("고민감정보 포함")

    conditions = [
        {
            "label": "① 동의 획득이 현실적으로 불가능하거나 연구 타당성에 심각한 영향",
            "met": cond1_met,
            "detail": cond1_detail,
        },
        {
            "label": "② 동의 거부 추정 사유가 없고 연구대상자 위험이 극히 낮음",
            "met": cond2_met,
            "detail": " / ".join(parts),
        },
    ]

    notes: list[str] = []
    if facts.deid_level == "pseudonymized":
        notes.append(
            "가명정보를 과학적 연구 목적으로 처리하는 경우 개인정보 보호법 제28조의2에 따라 "
            "정보주체 동의 없이 처리 가능하나, 생명윤리법상 IRB 심의(동의 면제 승인)는 별도로 필요"
        )
    if facts.is_identifiable:
        notes.append("식별가능 정보를 동의 없이 이용하려면 동의 면제 필요성이 매우 엄격히 소명되어야 함")

    if cond1_met and cond2_met:
        status, label = "eligible", "면제 요건 충족(권고)"
    elif not reasons and not facts.waiver_impracticable_detail.strip():
        status, label = "insufficient", "면제 사유 미기재"
    elif facts.is_identifiable and not cond2_met:
        status, label = "not_eligible", "면제 요건 미충족"
    else:
        status, label = "insufficient", "소명 보완 필요"

    return {
        "plan": "waiver",
        "status": status,
        "status_label": label,
        "conditions": conditions,
        "notes": notes,
        "legal": legal,
    }
