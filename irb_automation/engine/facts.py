"""사전검토 엔진 입력값.

DB 모델(Protocol)과 독립적으로 동작하도록 엔진은 ProtocolFacts만 받는다.
REST API의 무상태 평가(/api/prereview/evaluate)도 같은 구조를 사용한다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import date
from typing import Any

# 과제 스냅샷/엔진 입력으로 쓰이는 Protocol 필드 목록
PROTOCOL_FIELDS: tuple[str, ...] = (
    "title",
    "department",
    "funding_source",
    "study_type",
    "objectives",
    "background",
    "methods",
    "target_population",
    "inclusion_criteria",
    "exclusion_criteria",
    "expected_subjects",
    "sample_size_justification",
    "study_start",
    "study_end",
    "data_sources",
    "existing_data_only",
    "direct_contact",
    "data_period_start",
    "data_period_end",
    "data_items",
    "identifiers_collected",
    "identifier_justification",
    "sensitive_categories",
    "vulnerable_populations",
    "unstructured_data",
    "rare_disease",
    "deid_level",
    "deid_performed_by",
    "storage_location",
    "access_controls",
    "external_transfer",
    "external_recipient",
    "international_transfer",
    "data_linkage",
    "linkage_target",
    "retention_years",
    "destruction_method",
    "ai_ml_development",
    "commercial_use",
    "consent_plan",
    "waiver_impracticable_reasons",
    "waiver_impracticable_detail",
    "waiver_no_presumed_refusal",
    "waiver_minimal_risk_detail",
    "conflict_of_interest",
    "coi_detail",
    "multicenter",
    "review_model",
    "participating_sites",
    "external_irb",
    "custom_fields",
)


@dataclass
class ProtocolFacts:
    title: str = ""
    department: str = ""
    funding_source: str = ""
    study_type: str = "retrospective_chart_review"
    objectives: str = ""
    background: str = ""
    methods: str = ""
    target_population: str = ""
    inclusion_criteria: str = ""
    exclusion_criteria: str = ""
    expected_subjects: int = 0
    sample_size_justification: str = ""
    study_start: date | None = None
    study_end: date | None = None
    data_sources: list[str] = field(default_factory=list)
    existing_data_only: bool = True
    direct_contact: bool = False
    data_period_start: date | None = None
    data_period_end: date | None = None
    data_items: str = ""
    identifiers_collected: list[str] = field(default_factory=list)
    identifier_justification: str = ""
    sensitive_categories: list[str] = field(default_factory=list)
    vulnerable_populations: list[str] = field(default_factory=list)
    unstructured_data: list[str] = field(default_factory=list)
    rare_disease: bool = False
    deid_level: str = "pseudonymized"
    deid_performed_by: str = "data_steward"
    storage_location: str = "internal_secure"
    access_controls: str = ""
    external_transfer: bool = False
    external_recipient: str = ""
    international_transfer: bool = False
    data_linkage: bool = False
    linkage_target: str = ""
    retention_years: int = 3
    destruction_method: str = ""
    ai_ml_development: bool = False
    commercial_use: bool = False
    consent_plan: str = "waiver"
    waiver_impracticable_reasons: list[str] = field(default_factory=list)
    waiver_impracticable_detail: str = ""
    waiver_no_presumed_refusal: bool = False
    waiver_minimal_risk_detail: str = ""
    conflict_of_interest: bool = False
    coi_detail: str = ""
    multicenter: bool = False
    review_model: str = "local"
    participating_sites: str = ""
    external_irb: str = ""
    custom_fields: dict[str, Any] = field(default_factory=dict)  # 기관 추가 항목 값

    # 기관 양식 맥락(forms 패키지가 채움)
    custom_field_defs: list[dict[str, Any]] = field(default_factory=list)
    online_forms_pending: list[dict[str, Any]] = field(default_factory=list)

    # 제출 맥락
    submission_kind: str = "initial"
    previous_review_type: str | None = None
    uploaded_doc_types: set[str] = field(default_factory=set)
    pi_training_expires_on: date | None = None
    team_training: list[dict[str, Any]] = field(default_factory=list)  # [{name, expires_on}]
    protocol_expires_at: date | None = None
    report: dict[str, Any] = field(default_factory=dict)
    reference_date: date = field(default_factory=date.today)

    # ---------------------------------------------------------------- 파생 속성
    @property
    def is_identifiable(self) -> bool:
        return self.deid_level == "identifiable"

    @property
    def is_anonymized(self) -> bool:
        return self.deid_level == "anonymized"

    @property
    def has_identifiers(self) -> bool:
        return bool(self.identifiers_collected)

    @property
    def effectively_identifiable(self) -> bool:
        """연구자가 식별정보에 접근 가능한지 여부."""
        if self.is_identifiable or self.has_identifiers:
            return True
        return self.deid_level == "pseudonymized" and self.deid_performed_by == "researcher"

    @classmethod
    def from_mapping(cls, data: dict[str, Any], **context: Any) -> ProtocolFacts:
        names = {f.name for f in fields(cls)}
        values: dict[str, Any] = {}
        for key, value in {**data, **context}.items():
            if key not in names or value is None:
                continue
            if key in {
                "study_start",
                "study_end",
                "data_period_start",
                "data_period_end",
                "pi_training_expires_on",
                "protocol_expires_at",
                "reference_date",
            } and isinstance(value, str):
                value = date.fromisoformat(value) if value else None
                if value is None:
                    continue
            if key == "uploaded_doc_types":
                value = set(value)
            values[key] = value
        return cls(**values)

    @classmethod
    def from_protocol(cls, protocol: Any, **context: Any) -> ProtocolFacts:
        data = {name: getattr(protocol, name) for name in PROTOCOL_FIELDS}
        facts = cls.from_mapping(data, **context)
        _attach_institution_forms(facts, protocol)
        return facts

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["uploaded_doc_types"] = sorted(self.uploaded_doc_types)
        return out


def snapshot_protocol(protocol: Any) -> dict[str, Any]:
    """JSON 직렬화 가능한 과제 스냅샷.

    기관 추가 항목이 있으면 제출 시점의 항목 라벨·선택지(custom_field_meta)를 함께 남겨,
    이후 양식이 바뀌어도 과거 제출본과 변경 대비표를 그대로 해석할 수 있게 한다.
    """
    snap: dict[str, Any] = {}
    for name in PROTOCOL_FIELDS:
        value = getattr(protocol, name)
        if isinstance(value, date):
            value = value.isoformat()
        if name == "custom_fields":
            value = dict(value or {})
        snap[name] = value
    if snap.get("custom_fields"):
        meta = _custom_field_meta(protocol)
        if meta:
            snap["custom_field_meta"] = meta
    return snap


def _session_of(protocol: Any) -> Any:
    try:
        from sqlalchemy.orm import object_session

        return object_session(protocol)
    except Exception:  # 매핑되지 않은 객체(테스트용 더미 등)
        return None


def _custom_field_meta(protocol: Any) -> dict[str, Any]:
    db = _session_of(protocol)
    if db is None:
        return {}
    from ..forms.schema import custom_field_meta

    return custom_field_meta(db)


def _attach_institution_forms(facts: ProtocolFacts, protocol: Any) -> None:
    """기관 양식 정보(필수 추가 항목 정의, 웹 작성 서식 진행 상황)를 덧붙인다. DB 세션에 연결된 과제에서만 동작."""
    db = _session_of(protocol)
    if db is None:
        return
    from ..forms.online import pending_online_forms
    from ..forms.schema import active_protocol_fields

    facts.custom_field_defs = [f.to_dict() for f in active_protocol_fields(db)]
    pending, incomplete = pending_online_forms(db, protocol)
    facts.online_forms_pending = pending
    if incomplete:
        # 개인별 웹 서식이 일부만 제출된 서류는 체크리스트에서 '미제출'로 본다.
        facts.uploaded_doc_types = set(facts.uploaded_doc_types) - incomplete
