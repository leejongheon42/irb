"""HTML 폼 → 과제 데이터 변환."""
from __future__ import annotations

from typing import Any

from starlette.datastructures import FormData

from ..timeutil import parse_date

TEXT_FIELDS = (
    "title", "department", "funding_source", "study_type", "objectives", "background", "methods",
    "target_population", "inclusion_criteria", "exclusion_criteria", "sample_size_justification",
    "data_items", "identifier_justification", "deid_level", "deid_performed_by", "storage_location",
    "access_controls", "external_recipient", "linkage_target", "destruction_method", "consent_plan",
    "waiver_impracticable_detail", "waiver_minimal_risk_detail", "coi_detail",
    "review_model", "participating_sites", "external_irb",
)
INT_FIELDS = ("expected_subjects", "retention_years")
DATE_FIELDS = ("study_start", "study_end", "data_period_start", "data_period_end")
BOOL_FIELDS = (
    "existing_data_only", "direct_contact", "rare_disease", "external_transfer", "international_transfer",
    "data_linkage", "ai_ml_development", "commercial_use", "waiver_no_presumed_refusal", "conflict_of_interest",
    "multicenter",
)
LIST_FIELDS = (
    "data_sources", "identifiers_collected", "sensitive_categories", "vulnerable_populations",
    "unstructured_data", "waiver_impracticable_reasons",
)


def _int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def parse_protocol_form(form: FormData) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for f in TEXT_FIELDS:
        if f in form:
            data[f] = str(form.get(f) or "").strip()
    for f in INT_FIELDS:
        if f in form:
            data[f] = _int(form.get(f))
    for f in DATE_FIELDS:
        if f in form:
            data[f] = parse_date(str(form.get(f) or ""))
    # 체크박스는 폼 섹션 표시용 hidden 필드(_bools)가 있을 때만 해석
    if "_bools" in form:
        for f in BOOL_FIELDS:
            data[f] = form.get(f) in ("on", "true", "1", "yes")
    if "_lists" in form:
        for f in LIST_FIELDS:
            data[f] = [str(v) for v in form.getlist(f) if str(v).strip()]
    if "co_investigators" in form:
        data["co_investigators"] = sorted({int(v) for v in form.getlist("co_investigators") if str(v).isdigit()})
    if data.get("title") == "":
        data.pop("title")
    return data
