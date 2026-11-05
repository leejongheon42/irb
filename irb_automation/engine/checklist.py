"""제출 서류 완결성 자동 점검."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .facts import ProtocolFacts
from .ruleset import Ruleset

Condition = Callable[[ProtocolFacts, str], bool]

# 규칙 세트의 when 조건명 → 판정 함수 (임의 코드 실행 없이 화이트리스트로만 해석)
CONDITIONS: dict[str, Condition] = {
    "exempt": lambda f, rt: rt == "exempt",
    "not_exempt": lambda f, rt: rt != "exempt",
    "consent_obtain": lambda f, rt: f.consent_plan == "obtain",
    "consent_waiver": lambda f, rt: f.consent_plan != "obtain",
    "not_anonymized": lambda f, rt: f.deid_level != "anonymized",
    "identifiable": lambda f, rt: f.is_identifiable,
    "external_transfer": lambda f, rt: f.external_transfer,
    "data_linkage": lambda f, rt: f.data_linkage,
    "funded": lambda f, rt: bool(f.funding_source.strip()),
    "coi": lambda f, rt: f.conflict_of_interest,
    "ai_ml": lambda f, rt: f.ai_ml_development,
    "kind_initial": lambda f, rt: f.submission_kind in ("initial", "resubmission", "amendment"),
    "kind_continuing": lambda f, rt: f.submission_kind == "continuing",
    "kind_final_report": lambda f, rt: f.submission_kind == "final_report",
}

CONDITION_LABELS: dict[str, str] = {
    "exempt": "심의면제 대상",
    "not_exempt": "면제 외 심의",
    "consent_obtain": "동의 취득",
    "consent_waiver": "동의 면제 신청",
    "not_anonymized": "익명정보 아님",
    "identifiable": "식별정보 이용",
    "external_transfer": "외부 반출",
    "data_linkage": "데이터 결합",
    "funded": "연구비 지원",
    "coi": "이해상충 있음",
    "ai_ml": "AI 개발",
    "kind_initial": "신규·변경",
    "kind_continuing": "지속심의",
    "kind_final_report": "종료보고",
}

# 지속심의/종료보고 시에는 경과·종료 보고서 외 서류를 다시 요구하지 않는다.
_REPORT_KIND_DOCS = {"continuing": {"progress_report"}, "final_report": {"final_report"}}


def build_checklist(facts: ProtocolFacts, review_type: str, rs: Ruleset) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    only = _REPORT_KIND_DOCS.get(facts.submission_kind)
    for rule in rs.required_documents:
        doc = rule["doc"]
        conds = rule.get("when", []) or []
        unknown = [c for c in conds if c not in CONDITIONS]
        if unknown:
            raise ValueError(f"규칙 세트에 정의되지 않은 조건: {unknown}")
        required = all(CONDITIONS[c](facts, review_type) for c in conds)
        if only is not None:
            required = doc in only
        if not required:
            continue
        items.append(
            {
                "doc": doc,
                "label": rs.label("document_types", doc),
                "provided": doc in facts.uploaded_doc_types,
                "condition": " · ".join(CONDITION_LABELS.get(c, c) for c in conds) if conds else "공통",
            }
        )
    return items
