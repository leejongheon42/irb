"""서식에 채울 수 있는 필드 목록과 값 해석기.

필드 경로
- 표준 과제 필드: ``title``, ``objectives``, ``data_period_start`` …(카탈로그 코드는 표시명으로 바뀜, 코드는 ``.code``)
- 계산 필드: ``pi_name``, ``team``(반복용 목록), ``study_period``, ``consent_text`` …
- ``auto.*``   : 현재 내용 기준 자동 사전검토 결과(권고 심의유형 등)
- ``custom.*`` : 기관 추가 항목(과제의 custom_fields)
- ``profile.*``: 기관 정보(기관명·위원회명 등)

형식 지정(``{{필드|형식}}`` 또는 매핑의 ``경로|형식``)
- 날짜: ``YYYY년 MM월 DD일``, ``YYYY.MM.DD``, ``한글``(=YYYY년 MM월 DD일)
- 목록: ``줄바꿈``/``lines``, ``글머리``/``bullets``, ``번호``/``numbered`` (기본: 쉼표)
- 예/아니오: ``check``(■/□), ``ox``(O/X), ``유무``, ``있음없음`` (기본: 예/아니오)
- 숫자: ``raw``(천 단위 구분 없음)
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..engine.ruleset import get_ruleset

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


@dataclass(frozen=True)
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


def custom_fields(db: Session) -> list[FieldInfo]:
    """기관 추가 항목(Fork A: forms.schema.active_protocol_fields)이 있으면 목록에 포함."""
    try:
        from ..forms.schema import active_protocol_fields  # type: ignore[import-not-found]
    except ImportError:
        return []
    kinds = {"date": "date", "number": "number", "integer": "number", "bool": "bool", "boolean": "bool",
             "checkbox": "bool", "multiselect": "list", "multi": "list", "textarea": "longtext"}
    out = []
    for fd in active_protocol_fields(db) or []:
        key = getattr(fd, "key", None)
        if not key:
            continue
        out.append(_f(f"custom.{key}", getattr(fd, "label", key) or key, G_CUSTOM,
                      kinds.get(str(getattr(fd, "type", "text")).lower(), "text")))
    return out


def available_fields(db: Session, include_special: bool = True) -> list[FieldInfo]:
    fields = list(STANDARD_FIELDS) + custom_fields(db)
    return fields + (SPECIAL_FIELDS if include_special else [])


def is_known_path(path: str) -> bool:
    base = path[:-5] if path.endswith(".code") else path
    return base in _BY_PATH or base.startswith(("custom.", "profile.", "auto.")) or path in (SPECIAL_BLANK, SPECIAL_KEEP)


def field_kind(path: str) -> str:
    info = _BY_PATH.get(path)
    return info.kind if info else "text"


# ---------------------------------------------------------------- 형식
_DATE_TOKEN = re.compile(r"YYYY|YY|MM|M|DD|D")
_BOOL_PAIRS = {
    "check": ("■", "□"), "checkbox": ("■", "□"), "체크": ("■", "□"),
    "ox": ("O", "X"), "v": ("✔", ""),
    "yesno": ("예", "아니오"), "예아니오": ("예", "아니오"),
    "유무": ("유", "무"), "있음없음": ("있음", "없음"),
}


def format_date(value: date, fmt: str = "") -> str:
    if not fmt:
        return value.isoformat()
    if fmt.lower() in ("korean", "한글", "kor"):
        return f"{value.year}년 {value.month:02d}월 {value.day:02d}일"
    if not _DATE_TOKEN.search(fmt):
        return value.isoformat()
    tokens = {"YYYY": f"{value.year:04d}", "YY": f"{value.year % 100:02d}", "MM": f"{value.month:02d}",
              "M": str(value.month), "DD": f"{value.day:02d}", "D": str(value.day)}
    return _DATE_TOKEN.sub(lambda m: tokens[m.group(0)], fmt)


def _item_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("label") or item.get("name") or "")
    return format_value(item)


def format_value(value: Any, fmt: str = "") -> str:
    fmt = (fmt or "").strip()
    key = fmt.lower()
    if value is None:
        return ""
    if isinstance(value, bool):
        yes, no = _BOOL_PAIRS.get(key, _BOOL_PAIRS["yesno"])
        return yes if value else no
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return format_date(value, fmt)
    if isinstance(value, int | float):
        return str(value) if key == "raw" else f"{value:,}"
    if isinstance(value, list):
        items = [x for x in (_item_text(v) for v in value) if x]
        if key in ("lines", "줄바꿈"):
            return "\n".join(items)
        if key in ("bullets", "글머리"):
            return "\n".join(f"• {x}" for x in items)
        if key in ("numbered", "번호"):
            return "\n".join(f"{i}. {x}" for i, x in enumerate(items, 1))
        return ", ".join(items)
    if isinstance(value, dict):
        return _item_text(value)
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    if key in ("upper", "대문자"):
        return text.upper()
    return text


# ---------------------------------------------------------------- 해석기
_TRUTHY = {"1", "true", "yes", "y", "o", "예", "있음", "유", "on"}


class FieldResolver:
    """과제 하나에 대한 필드 값 해석(지연 계산·캐시)."""

    def __init__(self, db: Session, protocol: Any):
        self.db = db
        self.p = protocol
        self.rs = get_ruleset()
        self._cache: dict[str, Any] = {}
        self._profile: dict[str, Any] | None = None
        self._auto: dict[str, Any] | None = None

    # ---- 공개 API
    def value(self, path: str) -> Any:
        if path not in self._cache:
            self._cache[path] = self._value(path)
        return self._cache[path]

    def raw(self, path: str) -> Any:
        """비교용 원시 값(카탈로그 코드)."""
        base = path[:-5] if path.endswith(".code") else path
        if base in CATALOG_FIELDS or base == "consent_plan":
            return getattr(self.p, base, None)
        return self.value(path)

    def compare(self, path: str, op: str, operand: str) -> bool:
        raw, shown = self.raw(path), self.value(path)
        target = operand.strip()
        if isinstance(raw, bool):
            return raw == (target.lower() in _TRUTHY)
        if isinstance(raw, list):
            pool = [str(x) for x in raw] + [str(x) for x in (shown if isinstance(shown, list) else [])]
            return any(target == x or (op == "~" and target in x) for x in pool)
        pool = [format_value(raw), format_value(shown)]
        if op == "~":
            return any(target and target in x for x in pool)
        return target in pool

    # ---- 내부
    def _labels(self, catalog: str, codes: Any) -> Any:
        if isinstance(codes, list):
            return [self.rs.label(catalog, c) for c in codes]
        return self.rs.label(catalog, codes) if codes else ""

    def _value(self, path: str) -> Any:
        if path in (SPECIAL_BLANK, SPECIAL_KEEP):
            return ""
        if path.startswith("custom."):
            return (getattr(self.p, "custom_fields", None) or {}).get(path[7:])
        if path.startswith("profile."):
            return self.profile().get(path[8:], "")
        if path.startswith("auto."):
            return self.auto().get(path[5:], "")
        if path.endswith(".code"):
            return getattr(self.p, path[:-5], None)
        method = getattr(self, f"_c_{path}", None)
        if method is not None:
            return method()
        if path in CATALOG_FIELDS:
            return self._labels(CATALOG_FIELDS[path], getattr(self.p, path, None))
        if path == "consent_plan":
            return CONSENT_LABELS.get(self.p.consent_plan, self.p.consent_plan)
        if path in _BY_PATH and hasattr(self.p, path):
            return getattr(self.p, path)
        return None

    def profile(self) -> dict[str, Any]:
        if self._profile is None:
            settings = get_settings()
            data: dict[str, Any] = {"institution_name": settings.institution_name, "irb_name": settings.irb_name,
                                    "contact": "", "letter_footer": "", "protocol_no_format": ""}
            try:
                from ..forms.profile import get_profile  # type: ignore[import-not-found]
            except ImportError:
                get_profile = None
            if get_profile is not None:
                prof = get_profile(self.db)
                for key in list(data):
                    val = prof.get(key) if isinstance(prof, dict) else getattr(prof, key, None)
                    if val:
                        data[key] = val
            self._profile = data
        return self._profile

    def auto(self) -> dict[str, Any]:
        if self._auto is None:
            from ..workflow import service as wf

            result = wf.preview(self.db, self.p)
            self._auto = {
                "review_type_label": result["review_type_label"],
                "risk_score": result["risk"]["score"],
                "risk_level_label": result["risk"]["level_label"],
                "consent_status": result["consent"]["status_label"],
            }
        return self._auto

    def _users(self) -> list[Any]:
        from ..models import User

        co = [self.db.get(User, int(uid)) for uid in (self.p.co_investigators or [])]
        return [u for u in [self.p.pi, *co] if u is not None]

    # ---- 계산 필드
    def _c_protocol_no(self) -> str:
        return self.p.protocol_no or ""

    def _c_status_label(self) -> str:
        return self.p.status.label

    def _c_funding_text(self) -> str:
        return self.p.funding_source.strip() or "없음"

    def _c_study_period(self) -> str:
        return _period(self.p.study_start, self.p.study_end)

    def _c_data_period(self) -> str:
        return _period(self.p.data_period_start, self.p.data_period_end)

    def _c_pi_name(self) -> str:
        return self.p.pi.full_name if self.p.pi else ""

    def _c_pi_department(self) -> str:
        return self.p.pi.department if self.p.pi else ""

    def _c_pi_email(self) -> str:
        return self.p.pi.email if self.p.pi else ""

    def _c_pi_training_expires(self) -> Any:
        return self.p.pi.training_expires_on if self.p.pi else None

    def _c_co_investigators(self) -> str:
        return ", ".join(f"{u.full_name}({u.department})" if u.department else u.full_name
                         for u in self._users()[1:]) or "없음"

    def _c_co_investigator_names(self) -> str:
        return ", ".join(u.full_name for u in self._users()[1:])

    def _c_team(self) -> list[dict[str, Any]]:
        return [
            {"name": u.full_name, "department": u.department, "email": u.email,
             "role": "연구책임자" if i == 0 else "공동연구자", "training_expires_on": u.training_expires_on}
            for i, u in enumerate(self._users())
        ]

    def _c_team_names(self) -> str:
        return ", ".join(u.full_name for u in self._users())

    def _c_expected_subjects_text(self) -> str:
        return f"{self.p.expected_subjects:,}명"

    def _c_data_items(self) -> list[str]:
        return [x.strip() for x in (self.p.data_items or "").splitlines() if x.strip()]

    def _c_data_item_rows(self) -> list[dict[str, Any]]:
        from ..documents import classify_item

        rows = []
        for name in self._c_data_items():
            dtype, category, handling = classify_item(name)
            rows.append({"name": name, "type": dtype, "category": category, "handling": handling})
        return rows

    def _c_identifiers_label(self) -> str:
        return ", ".join(self._labels("identifiers", self.p.identifiers_collected or [])) or "없음"

    def _c_identifier_rows(self) -> list[dict[str, Any]]:
        from ..documents import _HANDLING

        return [{"name": self.rs.label("identifiers", c), "handling": _HANDLING.get(c, ("직접 식별자", "삭제"))[1]}
                for c in self.p.identifiers_collected or []]

    def _c_sensitive_label(self) -> str:
        return ", ".join(self._labels("sensitive_categories", self.p.sensitive_categories or [])) or "없음"

    def _c_vulnerable_label(self) -> str:
        return ", ".join(self._labels("vulnerable_populations", self.p.vulnerable_populations or [])) or "없음"

    def _c_retention_text(self) -> str:
        return f"{self.p.retention_years}년"

    def _c_consent_text(self) -> str:
        return "서면동의 취득" if self.p.consent_plan == "obtain" else "서면동의 면제 신청"

    def _c_coi_text(self) -> str:
        if not self.p.conflict_of_interest:
            return "없음"
        return f"있음 - {self.p.coi_detail}" if self.p.coi_detail else "있음"

    def _c_participating_sites(self) -> list[str]:
        return [x.strip() for x in (self.p.participating_sites or "").splitlines() if x.strip()]

    def _c_review_type_label(self) -> str:
        return self.p.review_type.label if self.p.review_type else ""

    def _c_risk_level_label(self) -> str:
        labels = {lv["key"]: lv["label"] for lv in self.rs.risk_levels}
        return labels.get(self.p.risk_level or "", "")

    def _c_today(self) -> date:
        return date.today()

    def _c_year(self) -> str:
        return str(date.today().year)


def _period(start: date | None, end: date | None) -> str:
    if not start and not end:
        return ""
    return f"{start.isoformat() if start else ''} ~ {end.isoformat() if end else ''}"
