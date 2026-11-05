"""연구대상자 위험도 점수화.

각 요인의 가중치는 규칙 세트(risk_weights, catalogs.*.weight)에서 읽는다.
점수와 함께 어떤 요인이 몇 점을 기여했는지를 남겨 설명 가능하도록 한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .facts import ProtocolFacts
from .ruleset import Ruleset


@dataclass
class RiskResult:
    score: int
    level: str
    level_label: str
    factors: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "level": self.level,
            "level_label": self.level_label,
            "factors": self.factors,
        }


def _level_for(score: int, rs: Ruleset) -> tuple[str, str]:
    for level in rs.risk_levels:
        if score <= int(level["max"]):
            return level["key"], level["label"]
    last = rs.risk_levels[-1]
    return last["key"], last["label"]


def assess_risk(facts: ProtocolFacts, rs: Ruleset) -> RiskResult:
    w = rs.risk_weights
    factors: list[dict[str, Any]] = []

    def add(name: str, points: int, detail: str = "") -> None:
        if points:
            factors.append({"factor": name, "points": int(points), "detail": detail})

    # 1) 식별가능성
    ident = w.get("identifiability", {})
    add(
        "식별가능성",
        ident.get(facts.deid_level, 0),
        rs.label("deid_levels", facts.deid_level),
    )

    # 2) 수집 식별자
    if facts.identifiers_collected:
        pts = sum(rs.weight("identifiers", i) for i in facts.identifiers_collected)
        labels = ", ".join(rs.label("identifiers", i) for i in facts.identifiers_collected)
        add("수집 식별자", min(pts, w.get("identifier_cap", 20)), labels)

    # 3) 민감정보
    if facts.sensitive_categories:
        pts = sum(rs.weight("sensitive_categories", c) for c in facts.sensitive_categories)
        labels = ", ".join(rs.label("sensitive_categories", c) for c in facts.sensitive_categories)
        add("민감정보", min(pts, w.get("sensitive_cap", 30)), labels)

    # 4) 취약한 연구대상자
    if facts.vulnerable_populations:
        pts = sum(rs.weight("vulnerable_populations", v) for v in facts.vulnerable_populations)
        labels = ", ".join(rs.label("vulnerable_populations", v) for v in facts.vulnerable_populations)
        add("취약한 연구대상자", min(pts, w.get("vulnerable_cap", 20)), labels)

    # 5) 비정형 데이터(재식별 위험)
    if facts.unstructured_data:
        pts = sum(rs.weight("unstructured_data", u) for u in facts.unstructured_data)
        labels = ", ".join(rs.label("unstructured_data", u) for u in facts.unstructured_data)
        add("비정형 데이터", min(pts, w.get("unstructured_cap", 15)), labels)

    # 6) 연구 수행 방식 / 데이터 흐름
    if facts.direct_contact:
        add("연구대상자 직접 접촉", w.get("direct_contact", 0), "설문·면담·추가 자료수집")
    if facts.deid_level == "pseudonymized" and facts.deid_performed_by == "researcher":
        add("연구자 직접 가명처리", w.get("researcher_performs_deid", 0), "처리자·이용자 미분리")
    if facts.external_transfer:
        add("기관 외 반출", w.get("external_transfer", 0), facts.external_recipient or "수령기관 미기재")
        if facts.is_identifiable or facts.has_identifiers:
            add("식별정보 외부 반출", w.get("external_transfer_identifiable", 0))
    if facts.international_transfer:
        add("국외 이전", w.get("international_transfer", 0))
    if facts.data_linkage:
        add("데이터 결합", w.get("data_linkage", 0), facts.linkage_target or "결합 대상 미기재")
    if facts.storage_location in ("external_cloud", "external_institution"):
        add("외부 저장", w.get("external_storage", 0), rs.label("storage_locations", facts.storage_location))
    if facts.ai_ml_development:
        add("AI/ML 모델 개발", w.get("ai_ml_development", 0))
    if facts.commercial_use:
        add("상업적 활용", w.get("commercial_use", 0))
    if facts.rare_disease:
        add("희귀질환(소수 집단)", w.get("rare_disease", 0))
    if facts.retention_years and facts.retention_years > int(w.get("long_retention_years", 10)):
        add("장기 보관", w.get("long_retention", 0), f"{facts.retention_years}년")

    # 7) 규모
    for tier in sorted(w.get("sample_size", []), key=lambda t: -int(t["min"])):
        if facts.expected_subjects >= int(tier["min"]):
            add("대규모 데이터", tier["weight"], f"{facts.expected_subjects:,}명")
            break

    score = sum(f["points"] for f in factors)
    level, label = _level_for(score, rs)
    return RiskResult(score=score, level=level, level_label=label, factors=factors)
