"""(선택) Claude 기반 AI 사전검토 보조.

- 기본 비활성화(IRB_AI_REVIEW_ENABLED=true 로 활성화).
- 연구계획서의 텍스트 항목과 규칙 엔진 결과만 전송하며, 환자 데이터는 절대 전송하지 않는다.
- 외부 API 전송이므로 기관 정보보호 부서의 사전 승인을 받은 뒤 활성화해야 한다.
- 결과는 심의위원 '참고용'으로만 표시되며 자동 판정에 사용되지 않는다.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ..ai.client import AIError, structured_call_with_response
from ..ai.privacy import mask_text, record_transmission
from ..config import get_settings

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """당신은 대한민국 병원 기관생명윤리위원회(IRB)의 사전검토를 돕는 보조자입니다.
의료데이터(의무기록, CDW, 의료영상 등)를 2차 활용하는 연구의 연구계획서를 읽고,
생명윤리 및 안전에 관한 법률, 개인정보 보호법(가명정보 특례 포함), 보건의료데이터 활용 가이드라인의
관점에서 심의위원이 확인해야 할 사항을 정리합니다.

원칙:
- 규칙 엔진이 이미 찾은 항목은 반복하지 말고, 서술 내용의 논리적 불일치나 누락을 찾는 데 집중하십시오.
  예: 목적과 수집 변수의 불일치, 목적에 비해 과도한 변수 수집, 방법에 기술된 데이터 흐름과
  보관·반출 항목의 불일치, 선정기준과 대상자 수의 불일치.
- 근거가 계획서 본문에 있는 경우에만 지적하고, 추측은 하지 마십시오.
- 최종 판단은 IRB가 하므로 승인/부결 결론을 내리지 마십시오.
- 모든 응답은 한국어로 작성하십시오."""

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "연구 개요 3~5문장 요약"},
        "consistency_issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "issue": {"type": "string"},
                    "evidence": {"type": "string"},
                    "suggestion": {"type": "string"},
                },
                "required": ["issue", "evidence", "suggestion"],
                "additionalProperties": False,
            },
        },
        "data_minimization": {
            "type": "array",
            "items": {"type": "string"},
            "description": "목적 대비 과도해 보이는 수집 항목",
        },
        "questions_for_researcher": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "consistency_issues", "data_minimization", "questions_for_researcher"],
    "additionalProperties": False,
}

TEXT_FIELDS = (
    "title",
    "objectives",
    "background",
    "methods",
    "target_population",
    "inclusion_criteria",
    "exclusion_criteria",
    "sample_size_justification",
    "data_items",
    "identifier_justification",
    "access_controls",
    "destruction_method",
    "waiver_impracticable_detail",
    "waiver_minimal_risk_detail",
)


def _build_prompt(snapshot: dict[str, Any], rule_result: dict[str, Any]) -> str:
    plan = {k: snapshot.get(k, "") for k in TEXT_FIELDS}
    structured = {
        k: snapshot.get(k)
        for k in (
            "study_type",
            "expected_subjects",
            "data_sources",
            "data_period_start",
            "data_period_end",
            "identifiers_collected",
            "sensitive_categories",
            "vulnerable_populations",
            "unstructured_data",
            "deid_level",
            "storage_location",
            "external_transfer",
            "international_transfer",
            "data_linkage",
            "retention_years",
            "ai_ml_development",
            "commercial_use",
            "consent_plan",
        )
    }
    engine = {
        "review_type": rule_result.get("review_type"),
        "risk": rule_result.get("risk", {}).get("score"),
        "findings": [f["message"] for f in rule_result.get("findings", [])],
    }
    return (
        "<protocol_text>\n"
        + json.dumps(plan, ensure_ascii=False, indent=2)
        + "\n</protocol_text>\n<structured_fields>\n"
        + json.dumps(structured, ensure_ascii=False, indent=2, default=str)
        + "\n</structured_fields>\n<rule_engine_result>\n"
        + json.dumps(engine, ensure_ascii=False, indent=2)
        + "\n</rule_engine_result>\n\n"
        "위 연구계획서를 검토하여 지정된 JSON 형식으로 응답하십시오."
    )


def run_ai_review(
    snapshot: dict[str, Any],
    rule_result: dict[str, Any],
    *,
    db: Any = None,
    user: Any = None,
    entity_id: int | None = None,
) -> dict[str, Any] | None:
    """AI 사전검토 보조. 비활성이면 None, 실패하면 {"error": ...}.

    연구계획서 텍스트는 개인정보 가드(마스킹)를 거쳐 전송하며, db 가 주어지면 전송 사실을 감사 로그에 남긴다.
    """
    if not get_settings().ai_enabled:
        return None
    guard = mask_text(_build_prompt(snapshot, rule_result))
    try:
        data, response = structured_call_with_response(
            system=SYSTEM_PROMPT, content=guard.text, schema=OUTPUT_SCHEMA, effort="high", purpose="사전검토 보조",
        )
    except AIError as e:  # AIUnavailable 포함
        return {"error": str(e)}
    if db is not None:
        record_transmission(db, user, "사전검토 보조", "protocol", entity_id, guard.text, "text", guard.masked)
    data["model"] = response.model
    if guard.masked:
        data["masked"] = guard.masked
    return data
