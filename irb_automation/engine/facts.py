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
        return cls.from_mapping(data, **context)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["uploaded_doc_types"] = sorted(self.uploaded_doc_types)
        return out


def snapshot_protocol(protocol: Any) -> dict[str, Any]:
    """JSON 직렬화 가능한 과제 스냅샷."""
    snap: dict[str, Any] = {}
    for name in PROTOCOL_FIELDS:
        value = getattr(protocol, name)
        if isinstance(value, date):
            value = value.isoformat()
        snap[name] = value
    return snap
