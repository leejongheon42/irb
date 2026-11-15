"""서식 필드 정의."""
from dataclasses import dataclass

SPECIAL_BLANK = "__blank__"
SPECIAL_KEEP = "__keep__"

CATALOG_FIELDS = {
    "study_type": "study_types",
    "data_sources": "data_sources",
    "identifiers_collected": "identifiers",
    "sensitive_categories": "sensitive_categories",
    "vulnerable_populations": "vulnerable_populations",
    "unstructured_data": "unstructured_data",
    "deid_level": "deid_levels",
    "deid_performed_by": "deid_performers",
    "storage_location": "storage_locations",
    "waiver_impracticable_reasons": "waiver_reasons",
    "review_model": "review_models",
}
CONSENT_LABELS = {"obtain": "서면동의 취득", "waiver": "서면동의 면제"}


@dataclass(frozen=True, slots=True)
class FieldInfo:
    path: str
    label: str
    group: str
    kind: str = "text"  # text | longtext | number | date | bool | list | choice
    example: str = ""


def _f(path: str, label: str, group: str, kind: str = "text", example: str = "") -> FieldInfo:
    return FieldInfo(path, label, group, kind, example)


G_BASIC, G_TEAM, G_CONTENT, G_DATA = "기본 정보", "연구진", "연구 내용", "활용 데이터"
G_PRIVACY, G_CONSENT, G_MULTI, G_REVIEW = "개인정보 보호", "동의·이해상충", "다기관", "심의 정보"
G_AUTO, G_DATE, G_PROFILE, G_CUSTOM, G_SPECIAL = "자동 사전검토", "날짜", "기관 정보", "기관 추가 항목", "특수"

STANDARD_FIELDS: list[FieldInfo] = [
    _f("committee", "심의위원회", G_BASIC),
    _f("access_route", "공용 IRB 이용 경로", G_BASIC),
    _f("research_category", "연구 분류", G_BASIC),
    _f("research_method", "연구 방법", G_BASIC, "list"),
    _f("subject_age_min", "대상자 최소 연령", G_DATA, "number"),
    _f("subject_age_max", "대상자 최대 연령", G_DATA, "number"),
    _f("consent_routes", "동의 경로", G_CONSENT, "list"),
    _f("title", "연구 제목", G_BASIC, example="제2형 당뇨병 환자의 SGLT2 억제제 사용과 급성 신손상"),
    _f("protocol_no", "과제번호", G_BASIC, example="IRB-2026-0001"),
    _f("version", "버전", G_BASIC, "number", "1"),
    _f("status_label", "진행 상태", G_BASIC, example="작성중"),
    _f("department", "소속(과제)", G_BASIC, example="내분비내과"),
    _f("study_type", "연구 유형", G_BASIC, "choice", "후향적 의무기록 조사"),
    _f("funding_source", "연구비 지원기관", G_BASIC, example="한국연구재단"),
    _f("funding_text", "연구비 지원(없으면 '없음')", G_BASIC, example="없음"),
    _f("study_start", "연구 시작일", G_BASIC, "date", "2026-12-01"),
    _f("study_end", "연구 종료일", G_BASIC, "date", "2028-10-31"),
    _f("study_period", "연구 기간", G_BASIC, example="2026-12-01 ~ 2028-10-31"),
    _f("pi_name", "연구책임자 성명", G_TEAM, example="정연구"),
    _f("pi_department", "연구책임자 소속", G_TEAM, example="내분비내과"),
    _f("pi_email", "연구책임자 이메일", G_TEAM, example="pi@hospital.kr"),
    _f("pi_training_expires", "연구책임자 교육 이수 만료일", G_TEAM, "date", "2027-12-31"),
    _f("co_investigators", "공동연구자(성명·소속)", G_TEAM, example="한공동(영상의학과)"),
    _f("co_investigator_names", "공동연구자 성명", G_TEAM, example="한공동"),
    _f("team", "연구진 목록(반복 행용)", G_TEAM, "list", "item.name / item.department / item.role"),
    _f("team_names", "연구진 성명", G_TEAM, example="정연구, 한공동"),
    _f("objectives", "연구 목적", G_CONTENT, "longtext"),
    _f("background", "연구 배경", G_CONTENT, "longtext"),
    _f("methods", "연구 방법", G_CONTENT, "longtext"),
    _f("target_population", "연구 대상자", G_CONTENT, "longtext"),
    _f("inclusion_criteria", "선정 기준", G_CONTENT, "longtext"),
    _f("exclusion_criteria", "제외 기준", G_CONTENT, "longtext"),
    _f("expected_subjects", "예상 대상자 수(숫자)", G_CONTENT, "number", "8,500"),
    _f("expected_subjects_text", "예상 대상자 수(명)", G_CONTENT, example="8,500명"),
    _f("sample_size_justification", "대상자 수 산출 근거", G_CONTENT, "longtext"),
    _f("data_sources", "데이터 출처", G_DATA, "list", "임상데이터웨어하우스(CDW)"),
    _f("existing_data_only", "기존 자료만 이용", G_DATA, "bool", "예"),
    _f("direct_contact", "연구대상자 직접 접촉", G_DATA, "bool", "아니오"),
    _f("data_period_start", "자료 대상 기간 시작", G_DATA, "date", "2019-01-01"),
    _f("data_period_end", "자료 대상 기간 종료", G_DATA, "date", "2024-12-31"),
    _f("data_period", "자료 대상 기간", G_DATA, example="2019-01-01 ~ 2024-12-31"),
    _f("data_items", "수집 변수", G_DATA, "list", "연령, 성별, HbA1c"),
    _f("data_item_rows", "수집 변수 표(반복 행용)", G_DATA, "list", "item.name / item.category / item.handling"),
    _f("identifiers_collected", "제공 식별자", G_DATA, "list", "병원등록번호(환자번호)"),
    _f("identifiers_label", "제공 식별자(없으면 '없음')", G_DATA, example="없음"),
    _f("identifier_rows", "제공 식별자 표(반복 행용)", G_DATA, "list", "item.name / item.handling"),
    _f("identifier_justification", "식별자 수집 필요성", G_DATA, "longtext"),
    _f("sensitive_categories", "민감정보", G_DATA, "list", "정신질환 정보"),
    _f("sensitive_label", "민감정보(없으면 '없음')", G_DATA, example="없음"),
    _f("vulnerable_populations", "취약한 연구대상자", G_DATA, "list", "미성년자(만 19세 미만)"),
    _f("vulnerable_label", "취약한 연구대상자(없으면 '없음')", G_DATA, example="없음"),
    _f("unstructured_data", "비정형 데이터", G_DATA, "list", "자유기술(경과기록·판독문 등)"),
    _f("rare_disease", "희귀질환 연구", G_DATA, "bool", "아니오"),
    _f("deid_level", "개인정보 처리 수준", G_PRIVACY, "choice", "가명정보(추가정보 없이 식별 불가)"),
    _f("deid_performed_by", "가명처리 수행자", G_PRIVACY, "choice", "데이터관리자/CDW팀"),
    _f("storage_location", "분석·보관 장소", G_PRIVACY, "choice", "원내 분석전용 서버(폐쇄망)"),
    _f("access_controls", "안전조치", G_PRIVACY, "longtext"),
    _f("external_transfer", "기관 외 반출", G_PRIVACY, "bool", "아니오"),
    _f("external_recipient", "제공받는 기관", G_PRIVACY),
    _f("international_transfer", "국외 이전", G_PRIVACY, "bool", "아니오"),
    _f("data_linkage", "데이터 결합", G_PRIVACY, "bool", "아니오"),
    _f("linkage_target", "결합 대상", G_PRIVACY),
    _f("retention_years", "보관기간(년, 숫자)", G_PRIVACY, "number", "3"),
    _f("retention_text", "보관기간", G_PRIVACY, example="3년"),
    _f("destruction_method", "파기 방법", G_PRIVACY, "longtext"),
    _f("ai_ml_development", "AI/ML 개발", G_PRIVACY, "bool", "아니오"),
    _f("commercial_use", "상업적 활용", G_PRIVACY, "bool", "아니오"),
    _f("consent_plan", "동의 방식", G_CONSENT, "choice", "서면동의 면제"),
    _f("consent_text", "동의 방식(문장)", G_CONSENT, example="서면동의 면제 신청"),
    _f("waiver_impracticable_reasons", "동의 면제 사유", G_CONSENT, "list", "대상자 수가 많아 …"),
    _f("waiver_impracticable_detail", "동의 면제 사유 설명", G_CONSENT, "longtext"),
    _f("waiver_no_presumed_refusal", "동의 거부 추정 사유 없음", G_CONSENT, "bool", "예"),
    _f("waiver_minimal_risk_detail", "위험이 극히 낮은 근거", G_CONSENT, "longtext"),
    _f("conflict_of_interest", "이해상충 있음", G_CONSENT, "bool", "아니오"),
    _f("coi_detail", "이해상충 내용", G_CONSENT, "longtext"),
    _f("coi_text", "이해상충(없으면 '없음')", G_CONSENT, example="없음"),
    _f("multicenter", "다기관 연구", G_MULTI, "bool", "아니오"),
    _f("review_model", "심의 방식", G_MULTI, "choice", "본원 IRB 단독 심의"),
    _f("participating_sites", "참여기관", G_MULTI, "list", "○○병원 · 김연구 · EMR"),
    _f("external_irb", "대표(심의) IRB", G_MULTI),
    _f("review_type_label", "심의 유형(제출 후 확정)", G_REVIEW, example="신속심의"),
    _f("risk_score", "위험도 점수(제출 시)", G_REVIEW, "number", "8"),
    _f("risk_level_label", "위험도 등급(제출 시)", G_REVIEW, example="최소위험"),
    _f("approved_at", "승인일", G_REVIEW, "date"),
    _f("expires_at", "승인 만료일", G_REVIEW, "date"),
    _f("auto.review_type_label", "권고 심의유형(현재 내용)", G_AUTO, example="심의면제"),
    _f("auto.risk_score", "위험도 점수(현재 내용)", G_AUTO, "number", "8"),
    _f("auto.risk_level_label", "위험도 등급(현재 내용)", G_AUTO, example="최소위험"),
    _f("auto.consent_status", "동의 면제 평가", G_AUTO, example="면제 요건 충족(권고)"),
    _f("today", "작성일(오늘)", G_DATE, "date", "2026-11-05"),
    _f("year", "올해 연도", G_DATE, example="2026"),
    _f("profile.institution_name", "기관명", G_PROFILE, example="○○대학교병원"),
    _f("profile.irb_name", "위원회명", G_PROFILE, example="○○대학교병원 기관생명윤리위원회"),
    _f("profile.contact", "IRB 사무국 연락처", G_PROFILE, example="02-000-0000"),
    _f("profile.letter_footer", "문서 하단 문구", G_PROFILE),
]
SPECIAL_FIELDS = [
    _f(SPECIAL_BLANK, "빈칸으로 비움", G_SPECIAL),
    _f(SPECIAL_KEEP, "자리표시자 그대로 둠(수기 작성)", G_SPECIAL),
]
_BY_PATH = {f.path: f for f in STANDARD_FIELDS}

