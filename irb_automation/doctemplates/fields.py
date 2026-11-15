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
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session

from ..config import get_settings
from ..engine.ruleset import get_ruleset
from .field_catalog import (
    _BY_PATH,
    CATALOG_FIELDS,
    CONSENT_LABELS,
    G_CUSTOM,
    SPECIAL_BLANK,
    SPECIAL_FIELDS,
    SPECIAL_KEEP,
    STANDARD_FIELDS,
    FieldInfo,
    _f,
)


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
