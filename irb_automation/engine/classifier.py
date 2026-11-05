"""심의 유형(면제/신속/정규) 자동 분류.

- 심의면제: 생명윤리법 제15조제2항, 시행규칙 제13조
  (개인식별정보를 수집·기록하지 않는 연구로서 기존 자료·문서를 이용하는 연구 등)
- 신속심의: 최소위험 이하, 정규심의 유발 요인 없음
- 정규심의: 위험도 점수 초과 또는 고위험 조합 해당
"""
from __future__ import annotations

from typing import Any

from .facts import ProtocolFacts
from .risk import RiskResult
from .ruleset import Ruleset


def _criterion(label: str, met: bool, detail: str = "") -> dict[str, Any]:
    return {"label": label, "met": bool(met), "detail": detail}


def assess_exemption(facts: ProtocolFacts, rs: Ruleset) -> dict[str, Any]:
    opt = rs.review_policy.get("exemption", {})
    criteria: list[dict[str, Any]] = []

    criteria.append(
        _criterion(
            "기존 자료·문서만을 이용 (직접 접촉·추가 수집 없음)",
            facts.existing_data_only and not facts.direct_contact,
            "시행규칙 제13조제1항제3호",
        )
    )

    if facts.is_anonymized:
        non_ident = True
        detail = "익명정보만 이용"
    elif facts.deid_level == "pseudonymized":
        non_ident = bool(opt.get("allow_pseudonymized", True)) and facts.deid_performed_by != "researcher"
        detail = (
            "가명정보 - 연구자는 추가정보(매핑) 접근 불가"
            if non_ident
            else "가명정보이나 연구자가 직접 가명처리하거나 기관 정책상 면제 불허"
        )
    else:
        non_ident = False
        detail = "식별가능 정보 이용"
    non_ident = non_ident and not facts.has_identifiers
    if facts.has_identifiers:
        detail += " / 식별자 수집 예정"
    criteria.append(_criterion("개인식별정보를 수집·기록하지 않음", non_ident, detail))

    excluded_sensitive = sorted(set(facts.sensitive_categories) & set(opt.get("excluded_sensitive", [])))
    criteria.append(
        _criterion(
            "면제 제외 민감정보 미포함",
            not excluded_sensitive,
            ", ".join(rs.label("sensitive_categories", s) for s in excluded_sensitive),
        )
    )

    excluded_vulnerable = sorted(
        set(facts.vulnerable_populations) & set(opt.get("excluded_vulnerable", []))
    )
    criteria.append(
        _criterion(
            "취약한 연구대상자 미포함(기관 SOP)",
            not excluded_vulnerable,
            ", ".join(rs.label("vulnerable_populations", v) for v in excluded_vulnerable),
        )
    )

    if opt.get("exclude_international_transfer", True):
        criteria.append(_criterion("국외 이전 없음", not facts.international_transfer))
    if opt.get("exclude_data_linkage", True):
        criteria.append(_criterion("타 기관 데이터 결합 없음", not facts.data_linkage))

    eligible = all(c["met"] for c in criteria)
    return {"eligible": eligible, "criteria": criteria, "legal": rs.legal("bioethics_15_2")}


def full_board_triggers(facts: ProtocolFacts, risk: RiskResult, rs: Ruleset) -> list[str]:
    policy = rs.review_policy
    trig = policy.get("full_board_triggers", {})
    reasons: list[str] = []
    ident = facts.effectively_identifiable

    max_score = int(policy.get("expedited_max_score", 44))
    if risk.score > max_score:
        reasons.append(f"위험도 점수 {risk.score}점이 신속심의 기준({max_score}점)을 초과")

    if ident:
        vul = set(facts.vulnerable_populations) & set(trig.get("identifiable_with_vulnerable", []))
        if vul:
            reasons.append(
                "식별가능 정보 + 취약한 연구대상자("
                + ", ".join(rs.label("vulnerable_populations", v) for v in sorted(vul))
                + ")"
            )
        sens = set(facts.sensitive_categories) & set(trig.get("identifiable_with_sensitive", []))
        if sens:
            reasons.append(
                "식별가능 정보 + 고민감정보("
                + ", ".join(rs.label("sensitive_categories", s) for s in sorted(sens))
                + ")"
            )
        if trig.get("identifiable_international_transfer") and facts.international_transfer:
            reasons.append("식별가능 정보의 국외 이전")
        if trig.get("identifiable_commercial_use") and facts.commercial_use:
            reasons.append("식별가능 정보의 상업적 활용")

    if trig.get("direct_contact_with_vulnerable") and facts.direct_contact and facts.vulnerable_populations:
        reasons.append("취약한 연구대상자 직접 접촉")
    return reasons


def classify(facts: ProtocolFacts, risk: RiskResult, rs: Ruleset) -> dict[str, Any]:
    exemption = assess_exemption(facts, rs)
    triggers = full_board_triggers(facts, risk, rs)
    rationale: list[str] = []

    kind = facts.submission_kind
    if kind == "final_report":
        review_type = "expedited"
        rationale.append("종료보고는 신속 확인 절차로 처리")
    elif kind == "continuing" and facts.previous_review_type == "full" and triggers:
        review_type = "full"
        rationale.append("정규심의 과제의 지속심의로 고위험 요인이 유지됨")
    elif kind == "continuing":
        review_type = "expedited"
        rationale.append("지속심의 - 위험 요인 변동 없음을 전제로 신속심의")
    elif triggers:
        review_type = "full"
        rationale.extend(triggers)
    elif exemption["eligible"]:
        review_type = "exempt"
        rationale.append("심의면제 요건을 모두 충족 (IRB의 면제 확인 필요)")
    else:
        review_type = "expedited"
        unmet = [c["label"] for c in exemption["criteria"] if not c["met"]]
        rationale.append(f"위험도 {risk.level_label}({risk.score}점) - 정규심의 유발 요인 없음")
        if unmet:
            rationale.append("면제 미충족: " + "; ".join(unmet))

    if facts.review_model == "external" and kind not in ("continuing", "final_report"):
        rationale.append(
            f"타기관 IRB({facts.external_irb or '미기재'}) 승인 결과 인정 검토 - 본원 고유 사항(local context) 확인 필요"
        )
    elif facts.review_model == "lead" and facts.multicenter:
        rationale.append("본원이 대표 IRB로서 참여기관 전체를 심의 - 기관별 고유 사항 수렴 필요")

    return {
        "review_type": review_type,
        "exemption": exemption,
        "full_board_triggers": triggers,
        "rationale": rationale,
    }
