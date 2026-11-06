"""신청건 스냅샷 간 변경 대비표 자동 생성."""
from __future__ import annotations

from typing import Any

from ..engine.ruleset import Ruleset

FIELD_LABELS: dict[str, tuple[str, str | None]] = {
    # 필드: (표시명, 카탈로그)
    "title": ("연구 제목", None),
    "department": ("소속", None),
    "funding_source": ("연구비 지원", None),
    "study_type": ("연구 유형", "study_types"),
    "objectives": ("연구 목적", None),
    "background": ("연구 배경", None),
    "methods": ("연구 방법", None),
    "target_population": ("연구 대상자", None),
    "inclusion_criteria": ("선정 기준", None),
    "exclusion_criteria": ("제외 기준", None),
    "expected_subjects": ("예상 대상자 수", None),
    "sample_size_justification": ("대상자 수 산출 근거", None),
    "study_start": ("연구 시작일", None),
    "study_end": ("연구 종료일", None),
    "data_sources": ("데이터 출처", "data_sources"),
    "existing_data_only": ("기존 자료만 이용", None),
    "direct_contact": ("직접 접촉", None),
    "data_period_start": ("자료 기간 시작", None),
    "data_period_end": ("자료 기간 종료", None),
    "data_items": ("수집 변수", None),
    "identifiers_collected": ("제공 식별자", "identifiers"),
    "identifier_justification": ("식별자 수집 사유", None),
    "sensitive_categories": ("민감정보", "sensitive_categories"),
    "vulnerable_populations": ("취약한 연구대상자", "vulnerable_populations"),
    "unstructured_data": ("비정형 데이터", "unstructured_data"),
    "rare_disease": ("희귀질환", None),
    "deid_level": ("처리 수준", "deid_levels"),
    "deid_performed_by": ("가명처리 수행자", "deid_performers"),
    "storage_location": ("보관 장소", "storage_locations"),
    "access_controls": ("안전조치", None),
    "external_transfer": ("외부 반출", None),
    "external_recipient": ("제공받는 기관", None),
    "international_transfer": ("국외 이전", None),
    "data_linkage": ("데이터 결합", None),
    "linkage_target": ("결합 대상", None),
    "retention_years": ("보관기간(년)", None),
    "destruction_method": ("파기 방법", None),
    "ai_ml_development": ("AI 개발", None),
    "commercial_use": ("상업적 활용", None),
    "consent_plan": ("동의 방식", None),
    "waiver_impracticable_reasons": ("동의 면제 사유", "waiver_reasons"),
    "waiver_impracticable_detail": ("동의 면제 설명", None),
    "waiver_no_presumed_refusal": ("동의 거부 추정 사유 없음", None),
    "waiver_minimal_risk_detail": ("위험 극히 낮은 근거", None),
    "conflict_of_interest": ("이해상충", None),
    "coi_detail": ("이해상충 내용", None),
    "multicenter": ("다기관 연구", None),
    "review_model": ("심의 방식", "review_models"),
    "participating_sites": ("참여기관", None),
    "external_irb": ("대표 IRB", None),
}

# 위험·심의유형에 영향을 주는 핵심 항목(대비표에서 강조)
MATERIAL_FIELDS = {
    "identifiers_collected", "deid_level", "deid_performed_by", "sensitive_categories", "vulnerable_populations",
    "external_transfer", "international_transfer", "data_linkage", "storage_location", "expected_subjects",
    "data_period_start", "data_period_end", "consent_plan", "commercial_use", "ai_ml_development", "direct_contact",
    "unstructured_data", "review_model",
}


def _fmt(value: Any, catalog: str | None, rs: Ruleset) -> str:
    if value is None or value == "" or value == []:
        return "-"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, list):
        return ", ".join(rs.label(catalog, v) if catalog else str(v) for v in value)
    if catalog:
        return rs.label(catalog, str(value))
    if value == "obtain":
        return "서면동의 취득"
    if value == "waiver":
        return "동의 면제"
    return str(value)


def diff_snapshots(old: dict[str, Any], new: dict[str, Any], rs: Ruleset) -> list[dict[str, Any]]:
    changes = []
    for key, (label, catalog) in FIELD_LABELS.items():
        a, b = old.get(key), new.get(key)
        if isinstance(a, list) and isinstance(b, list):
            if sorted(map(str, a)) == sorted(map(str, b)):
                continue
        elif (a or None) == (b or None):
            continue
        changes.append(
            {
                "field": key,
                "label": label,
                "before": _fmt(a, catalog, rs),
                "after": _fmt(b, catalog, rs),
                "material": key in MATERIAL_FIELDS,
            }
        )
    changes.extend(_diff_custom(old, new))
    return changes


def _fmt_custom(meta: dict[str, Any], value: Any) -> str:
    if value is None or value == "" or value == []:
        return "-"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    opts = meta.get("options") or {}
    if isinstance(value, list):
        return ", ".join(opts.get(str(v), str(v)) for v in value)
    return opts.get(str(value), str(value))


def _diff_custom(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """기관 추가 항목 변경. 라벨·선택지는 각 제출본에 함께 저장된 메타(custom_field_meta)를 사용한다."""
    a_all, b_all = old.get("custom_fields") or {}, new.get("custom_fields") or {}
    meta = {**(old.get("custom_field_meta") or {}), **(new.get("custom_field_meta") or {})}
    order = list(meta) + sorted((set(a_all) | set(b_all)) - set(meta))
    out = []
    for key in order:
        a, b = a_all.get(key), b_all.get(key)
        if isinstance(a, list) and isinstance(b, list):
            if sorted(map(str, a)) == sorted(map(str, b)):
                continue
        elif (a if a is not None else "") == (b if b is not None else ""):
            continue
        m = meta.get(key, {})
        out.append({
            "field": f"custom__{key}",
            "label": f"[기관] {m.get('label', key)}",
            "before": _fmt_custom(m, a),
            "after": _fmt_custom(m, b),
            "material": bool(m.get("material")),
        })
    return out


def previous_snapshot(protocol, submission) -> dict[str, Any] | None:
    """해당 신청건 직전의 '내용 신청건'(신규·재제출·변경) 스냅샷."""
    prev = None
    for sub in protocol.submissions:
        if sub.id == submission.id:
            break
        if sub.kind.value in ("initial", "resubmission", "amendment"):
            prev = sub
    return prev.snapshot if prev else None
