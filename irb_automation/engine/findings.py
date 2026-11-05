"""규칙 기반 검토 의견(findings) 생성.

severity
 - critical: 접수 불가 수준(필수 정보·서류 누락, 법령 위반 소지)
 - major:    심의 전 보완 필요
 - minor:    권고 사항
 - info:     심의위원 참고 사항
"""
from __future__ import annotations

from datetime import date
from typing import Any

from .facts import ProtocolFacts
from .risk import RiskResult
from .ruleset import Ruleset

SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2, "info": 3}
SEVERITY_LABELS = {"critical": "필수 보완", "major": "보완 권고", "minor": "개선 제안", "info": "참고"}

MIN_TEXT = 30


class _Collector:
    def __init__(self, rs: Ruleset):
        self.rs = rs
        self.items: list[dict[str, Any]] = []

    def add(
        self,
        severity: str,
        code: str,
        message: str,
        recommendation: str = "",
        legal: str | None = None,
        field: str | None = None,
    ) -> None:
        self.items.append(
            {
                "severity": severity,
                "severity_label": SEVERITY_LABELS[severity],
                "code": code,
                "message": message,
                "recommendation": recommendation,
                "legal": self.rs.legal(legal) if legal else "",
                "field": field or "",
            }
        )


def _short(text: str) -> bool:
    return len((text or "").strip()) < MIN_TEXT


def generate_findings(
    facts: ProtocolFacts,
    risk: RiskResult,
    classification: dict[str, Any],
    consent: dict[str, Any],
    checklist: list[dict[str, Any]],
    rs: Ruleset,
) -> list[dict[str, Any]]:
    c = _Collector(rs)
    today: date = facts.reference_date
    kind = facts.submission_kind

    # ---------------------------------------------------------------- 서류
    for item in checklist:
        if not item["provided"]:
            c.add(
                "critical",
                "DOC_MISSING",
                f"필수 서류 누락: {item['label']}",
                "해당 서류를 첨부하십시오.",
                field="documents",
            )

    # ---------------------------------------------------------------- 연구자 자격
    if facts.pi_training_expires_on is None:
        c.add("minor", "PI_TRAINING_UNKNOWN", "연구책임자의 연구윤리 교육 이수 정보가 등록되어 있지 않습니다.",
              "교육 이수일/만료일을 사용자 정보에 등록하십시오.")
    elif facts.pi_training_expires_on < today:
        c.add("major", "PI_TRAINING_EXPIRED",
              f"연구책임자의 연구윤리 교육 이수가 만료되었습니다({facts.pi_training_expires_on.isoformat()}).",
              "교육 재이수 후 이수증을 첨부하십시오.")

    expired_team = [t["name"] for t in facts.team_training if t.get("expires_on") and t["expires_on"] < today]
    missing_team = [t["name"] for t in facts.team_training if not t.get("expires_on")]
    if expired_team:
        c.add("major", "TEAM_TRAINING_EXPIRED",
              "공동연구자 연구윤리 교육 이수 만료: " + ", ".join(expired_team),
              "교육 재이수 후 이수증을 첨부하거나 연구진에서 제외하십시오.")
    if missing_team:
        c.add("minor", "TEAM_TRAINING_UNKNOWN",
              "공동연구자 교육 이수 정보 미등록: " + ", ".join(missing_team))

    if kind in ("continuing", "final_report"):
        _report_findings(c, facts, today)
        return _sorted(c.items)

    # ---------------------------------------------------------------- 연구계획 기본 정보
    if _short(facts.objectives):
        c.add("critical", "OBJECTIVES_MISSING", "연구 목적이 기재되지 않았거나 지나치게 간략합니다.",
              "연구 목적과 가설을 구체적으로 기술하십시오.", field="objectives")
    if _short(facts.methods):
        c.add("critical", "METHODS_MISSING", "연구 방법이 기재되지 않았거나 지나치게 간략합니다.",
              "자료 추출 방법, 분석 방법, 통계 계획을 기술하십시오.", field="methods")
    if _short(facts.inclusion_criteria):
        c.add("major", "INCLUSION_MISSING", "선정 기준이 구체적이지 않습니다.",
              "진단코드, 기간, 연령 등 선정 기준을 명확히 하십시오.", field="inclusion_criteria")
    if not facts.data_items.strip():
        c.add("critical", "DATA_ITEMS_MISSING", "수집 변수(자료수집 항목)가 기재되지 않았습니다.",
              "연구 목적 달성에 필요한 최소한의 변수 목록을 작성하십시오(최소수집 원칙).", field="data_items")
    if facts.expected_subjects <= 0:
        c.add("major", "SAMPLE_SIZE_MISSING", "예상 연구대상자 수가 기재되지 않았습니다.", field="expected_subjects")
    elif _short(facts.sample_size_justification):
        c.add("minor", "SAMPLE_SIZE_JUSTIFICATION", "대상자 수 산출 근거가 부족합니다.",
              "통계적 산출 근거 또는 전수조사 사유를 기재하십시오.", field="sample_size_justification")
    if facts.expected_subjects >= 100_000:
        c.add("info", "LARGE_DATASET",
              f"대규모 데이터({facts.expected_subjects:,}명) 활용 연구입니다.",
              "목적에 필요한 최소 범위의 데이터인지 확인하십시오.")

    # 기간
    if facts.study_start and facts.study_end and facts.study_end < facts.study_start:
        c.add("critical", "STUDY_PERIOD_INVALID", "연구 종료일이 시작일보다 앞섭니다.", field="study_end")
    if facts.data_period_start and facts.data_period_end:
        if facts.data_period_end < facts.data_period_start:
            c.add("critical", "DATA_PERIOD_INVALID", "자료 수집 대상 기간의 종료일이 시작일보다 앞섭니다.",
                  field="data_period_end")
        if facts.existing_data_only and facts.data_period_end > today:
            c.add("major", "DATA_PERIOD_FUTURE",
                  "기존 자료 이용 연구이나 자료 대상 기간이 미래 시점을 포함합니다.",
                  "후향적 연구는 심의 승인일 이전에 생성된 자료로 한정하십시오. "
                  "전향적 자료 수집이 포함되면 연구 유형을 변경하십시오.", field="data_period_end")
    else:
        c.add("major", "DATA_PERIOD_MISSING", "자료 수집 대상 기간이 기재되지 않았습니다.", field="data_period_start")

    # ---------------------------------------------------------------- 식별정보
    ids = set(facts.identifiers_collected)
    if "rrn" in ids:
        c.add("critical", "RRN_COLLECTION",
              "주민등록번호 수집이 계획되어 있습니다.",
              "주민등록번호는 법령상 근거가 있는 경우에만 처리 가능합니다. 연구용 일련번호로 대체하십시오.",
              legal="pipa_24_2", field="identifiers_collected")
    unique_ids = [i for i in ids - {"rrn"} if (rs.catalog("identifiers").get(i) or {}).get("unique_id")]
    if unique_ids:
        c.add("major", "UNIQUE_ID_COLLECTION",
              "고유식별정보 수집: " + ", ".join(rs.label("identifiers", i) for i in sorted(unique_ids)),
              "고유식별정보는 별도 동의 또는 법령 근거가 필요합니다.", legal="pipa_24")
    if ids and _short(facts.identifier_justification):
        c.add("major", "IDENTIFIER_JUSTIFICATION",
              "식별자를 수집하나 수집 필요성 소명이 부족합니다.",
              "각 식별자가 연구 목적상 반드시 필요한 이유를 기재하거나, 가명처리 후 제공받으십시오.",
              field="identifier_justification")
    if ids and facts.deid_level == "anonymized":
        c.add("critical", "DEID_INCONSISTENT",
              "익명정보를 이용한다고 기재했으나 식별자 수집이 계획되어 있습니다.",
              "개인정보 처리 수준 또는 수집 식별자 항목을 정정하십시오.", field="deid_level")
    if facts.deid_level == "pseudonymized" and facts.deid_performed_by == "researcher":
        c.add("major", "DEID_SEPARATION",
              "연구자가 원본 식별정보에 접근하여 직접 가명처리를 수행합니다.",
              "가명처리는 연구자와 분리된 데이터관리자(CDW팀 등)가 수행하고, "
              "추가정보(매핑정보)는 분리 보관하는 것을 권고합니다.",
              legal="pipa_28_4")
    if facts.deid_level == "pseudonymized":
        c.add("info", "PSEUDONYM_DUTIES",
              "가명정보 처리 시 재식별 금지, 추가정보 분리 보관, 처리기록 작성·보관 의무가 적용됩니다.",
              legal="pipa_28_5")

    # 비정형 데이터
    if "free_text" in facts.unstructured_data:
        c.add("major", "FREE_TEXT",
              "자유기술(경과기록·판독문 등) 데이터를 이용합니다.",
              "자유기술 내 성명·연락처·주소·등록번호 등 식별정보 제거(스크러빙) 절차를 기술하십시오.",
              legal="guideline", field="unstructured_data")
    if "medical_images" in facts.unstructured_data:
        c.add("minor", "MEDICAL_IMAGES",
              "의료영상(DICOM)을 이용합니다.",
              "DICOM 헤더 태그 비식별화, 영상 내 번인(burn-in) 텍스트 제거, "
              "두경부 CT/MRI의 경우 얼굴 재구성 방지(defacing) 계획을 확인하십시오.",
              legal="guideline")
    if "audio_video" in facts.unstructured_data:
        c.add("major", "AUDIO_VIDEO", "음성·동영상 자료는 그 자체로 식별 가능성이 높습니다.",
              "음성 변조·얼굴 블러 등 처리 방안 또는 동의 취득을 검토하십시오.")

    # 민감정보 / 취약군
    for cat in facts.sensitive_categories:
        c.add("info", f"SENSITIVE_{cat.upper()}",
              f"민감정보 포함: {rs.label('sensitive_categories', cat)}",
              "접근권한 최소화 및 소규모 셀 공개 제한 등 추가 보호조치를 기술하십시오.",
              legal=(rs.catalog("sensitive_categories").get(cat) or {}).get("legal"))
    if "genetic" in facts.sensitive_categories and not facts.is_anonymized:
        c.add("major", "GENETIC_DATA",
              "유전정보를 식별 가능 또는 가명 상태로 이용합니다.",
              "유전정보는 재식별 위험이 높으므로 활용 범위·보관·결합 제한을 구체적으로 기술하고, "
              "생명윤리법상 유전자검사 관련 규정 적용 여부를 확인하십시오.")
    if facts.vulnerable_populations:
        labels = ", ".join(rs.label("vulnerable_populations", v) for v in facts.vulnerable_populations)
        sev = "major" if facts.effectively_identifiable else "info"
        c.add(sev, "VULNERABLE",
              f"취약한 연구대상자 포함: {labels}",
              "추가 보호 대책(최소 데이터, 접근 제한, 결과 공개 시 소규모 셀 억제 등)을 기술하십시오.",
              legal="bioethics_3_5")
    if facts.rare_disease:
        c.add("minor", "RARE_DISEASE",
              "희귀질환 대상 연구로 소수 집단에 따른 재식별 위험이 있습니다.",
              "결과 공표 시 소규모 셀(예: 5명 미만) 억제 및 지역·연령 범주화를 적용하십시오.",
              legal="guideline")

    # ---------------------------------------------------------------- 저장·반출·결합
    if not facts.is_anonymized and _short(facts.access_controls):
        c.add("major", "ACCESS_CONTROLS",
              "데이터 보관 시 접근통제·암호화 등 안전조치가 구체적으로 기술되지 않았습니다.",
              "분석 장소, 접근권한자, 암호화, 반출 통제, 로그 관리 방안을 기술하십시오.",
              legal="pipa_29", field="access_controls")
    if facts.storage_location in ("external_cloud", "external_institution"):
        c.add("major", "EXTERNAL_STORAGE",
              f"데이터를 원외({rs.label('storage_locations', facts.storage_location)})에 보관합니다.",
              "보안인증(예: CSAP/ISMS) 여부, 위탁계약, 접근통제 방안을 제시하십시오.")
    if facts.external_transfer:
        if not facts.external_recipient.strip():
            c.add("major", "TRANSFER_RECIPIENT", "외부 반출 시 자료를 제공받는 기관이 기재되지 않았습니다.",
                  field="external_recipient")
        c.add("info", "EXTERNAL_TRANSFER",
              "자료의 기관 외 제공이 포함됩니다.",
              "자료제공 계약서(재제공 금지, 목적 외 이용 금지, 파기 조항) 체결 여부를 확인하십시오.",
              legal="bioethics_18")
    if facts.international_transfer:
        c.add("major", "INTERNATIONAL_TRANSFER",
              "개인정보(가명정보 포함)의 국외 이전이 포함됩니다.",
              "국외 이전 요건 충족 여부(동의, 인증, 적정성 결정 등)와 이전 국가·기관을 명시하십시오.",
              legal="pipa_28_8")
    if facts.data_linkage:
        if not facts.linkage_target.strip():
            c.add("major", "LINKAGE_TARGET", "결합 대상 데이터(기관)가 기재되지 않았습니다.", field="linkage_target")
        c.add("major", "DATA_LINKAGE",
              "서로 다른 개인정보처리자 간 가명정보 결합이 포함됩니다.",
              "결합전문기관을 통한 결합 절차와 결합 후 반출 심사 계획을 기술하십시오.",
              legal="pipa_28_3")

    # 보관·파기
    if facts.retention_years <= 0:
        c.add("major", "RETENTION_MISSING", "자료 보관기간이 기재되지 않았습니다.", field="retention_years")
    if _short(facts.destruction_method) and not facts.is_anonymized:
        c.add("major", "DESTRUCTION_MISSING", "연구 종료 후 자료 파기 방법이 구체적이지 않습니다.",
              "파기 시점, 방법(복구 불가능한 삭제), 파기 확인 절차를 기술하십시오.", field="destruction_method")

    # AI / 상업적 활용
    if facts.ai_ml_development:
        c.add("info", "AI_ML",
              "AI/ML 모델 개발 연구입니다.",
              "학습 데이터의 가명처리 수준, 모델의 학습데이터 암기·추출 위험, "
              "모델 반출·공개 범위, 성능 검증 데이터 분리 계획을 확인하십시오.",
              legal="guideline")
    if facts.commercial_use:
        c.add("major", "COMMERCIAL_USE",
              "연구 결과 또는 데이터의 상업적 활용이 계획되어 있습니다.",
              "과학적 연구 목적 범위 내인지, 이해상충 및 이익 공유 계획을 명확히 하십시오.",
              legal="pipa_28_2")

    # 직접 접촉
    if facts.direct_contact or not facts.existing_data_only:
        c.add("info", "DIRECT_CONTACT",
              "연구대상자 직접 접촉 또는 전향적 자료수집이 포함됩니다.",
              "의료데이터 2차 활용 범위를 넘어서므로 동의 절차 및 연구대상자 보호 방안을 별도 검토해야 합니다.")

    # ---------------------------------------------------------------- 동의
    if consent["plan"] == "waiver":
        if consent["status"] == "insufficient":
            sev = "critical" if not facts.waiver_impracticable_reasons else "major"
            c.add(sev, "WAIVER_JUSTIFICATION",
                  "동의 면제를 신청했으나 면제 요건 소명이 부족합니다.",
                  "동의 획득이 불가능한 구체적 사유와 연구대상자 위험이 극히 낮은 근거를 기재하십시오.",
                  legal="bioethics_16_3", field="waiver_impracticable_detail")
        elif consent["status"] == "not_eligible":
            c.add("critical", "WAIVER_NOT_ELIGIBLE",
                  "현재 계획으로는 동의 면제 요건 충족이 어렵습니다.",
                  "가명처리 수준을 높이거나 동의 취득 방식으로 변경하는 것을 검토하십시오.",
                  legal="bioethics_16_3")

    # ---------------------------------------------------------------- 이해상충
    if facts.conflict_of_interest and _short(facts.coi_detail):
        c.add("major", "COI_DETAIL", "이해상충이 있다고 신고했으나 세부 내용이 부족합니다.",
              "이해관계 내용과 관리 방안을 기재하십시오.", field="coi_detail")
    if facts.funding_source.strip() and facts.commercial_use and not facts.conflict_of_interest:
        c.add("minor", "COI_CHECK", "연구비 지원과 상업적 활용이 있으나 이해상충이 '없음'으로 신고되었습니다.",
              "이해상충 여부를 재확인하십시오.")

    # ---------------------------------------------------------------- 다기관/위임심의
    if facts.multicenter and not facts.participating_sites.strip():
        c.add("major", "SITES_MISSING", "다기관 연구이나 참여기관이 기재되지 않았습니다.",
              "참여기관과 기관별 연구책임자, 각 기관의 데이터 제공 범위를 기재하십시오.",
              field="participating_sites")
    if facts.review_model == "external":
        if not facts.external_irb.strip():
            c.add("major", "EXTERNAL_IRB_MISSING", "심의 결과를 인정할 타기관(대표) IRB가 기재되지 않았습니다.",
                  field="external_irb")
        c.add("info", "RELIANCE",
              "타기관 IRB 심의 결과 인정(위임심의) 신청입니다.",
              "대표 IRB 승인서·승인 계획서 버전 일치 여부와 본원 고유 사항(자료 반출 범위, 보안, 기관 규정)을 확인하십시오.",
              legal="bioethics_12")
    if facts.multicenter and facts.data_linkage:
        c.add("info", "MULTICENTER_LINKAGE",
              "다기관 자료를 결합하는 연구입니다.",
              "기관별 가명처리 기준을 통일하고 결합키 생성·결합 장소(결합전문기관)를 명시하십시오.",
              legal="pipa_28_3")

    if facts.submission_kind == "amendment":
        c.add("info", "AMENDMENT", "변경심의 신청입니다. 변경 전후 비교표를 확인하십시오.")

    return _sorted(c.items)


def _report_findings(c: _Collector, facts: ProtocolFacts, today: date) -> None:
    r = facts.report or {}
    if facts.submission_kind == "continuing":
        if facts.protocol_expires_at and facts.protocol_expires_at < today:
            c.add("major", "APPROVAL_LAPSED",
                  "승인 유효기간이 이미 만료되었습니다. 만료 이후의 연구 수행은 허용되지 않습니다.",
                  "만료 기간 중 수행된 연구 활동 여부를 보고하십시오.")
        if _short(str(r.get("progress_summary", ""))):
            c.add("major", "PROGRESS_SUMMARY", "연구 진행 경과 요약이 부족합니다.")
        try:
            analyzed = int(r.get("subjects_analyzed") or 0)
        except (TypeError, ValueError):
            analyzed = 0
        if facts.expected_subjects and analyzed > facts.expected_subjects:
            c.add("major", "SUBJECTS_EXCEEDED",
                  f"분석 대상자 수({analyzed:,})가 승인된 대상자 수({facts.expected_subjects:,})를 초과했습니다.",
                  "변경심의를 신청하십시오.")
    else:
        if not r.get("data_destroyed") and not facts.is_anonymized:
            c.add("major", "DATA_NOT_DESTROYED",
                  "연구 종료 보고이나 자료 파기 완료가 확인되지 않았습니다.",
                  "보관기간 및 파기 예정일을 명시하거나 파기 확인서를 첨부하십시오.")
        if _short(str(r.get("results_summary", ""))):
            c.add("minor", "RESULTS_SUMMARY", "연구 결과 요약이 부족합니다.")
    if r.get("deviations"):
        c.add("major", "DEVIATIONS", "연구계획 위반/이탈 사례가 보고되었습니다.",
              "위반 내용, 원인, 재발 방지 대책을 기술하십시오.")
    if r.get("data_breach"):
        c.add("critical", "DATA_BREACH",
              "개인정보 유출 또는 보안사고가 보고되었습니다.",
              "개인정보 보호책임자에게 즉시 보고하고 유출 통지·신고 절차를 이행하십시오.",
              legal="pipa_29")


def _sorted(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(items, key=lambda x: SEVERITY_ORDER[x["severity"]])
