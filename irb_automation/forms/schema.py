"""양식 스키마: 정의 검증, 버전 관리, 활성 양식 조회, 폼 값 해석.

스키마 구조(YAML/JSON):
  sections:
    - title: 구역 제목
      fields:
        - key: grant_number          # 영문 소문자·숫자·밑줄
          label: 연구비 과제번호
          type: text                 # text|textarea|number|date|select|multiselect|checkbox|yesno
          required: false
          help: 도움말
          placeholder: 예시
          options: [{value: a, label: 가}]   # select/multiselect
          show_when: {field: 다른키, equals: 값}  # equals 생략 시 '값이 있으면 표시'
          min: 0 / max: 100          # number
          material: false            # 변경 대비표에서 핵심 항목으로 강조
          prefill: user.full_name    # 웹 서식 전용 자동 채움
웹 서식(kind=online:<doc_type>)은 추가로 label, per_member, signer(any|pi), required, intro, pledge를 가진다.
"""
from __future__ import annotations

import keyword
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..engine.facts import PROTOCOL_FIELDS
from ..models import User
from .logic import FIELD_TYPES, coerce, option_map
from .models import FormDefinition

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
KIND_RE = re.compile(r"^(protocol|online:[a-z][a-z0-9_]{1,40})$")
BASE_RESERVED = {
    "id", "csrf_token", "custom_fields", "password", "attest", "user_id", "status", "version", "protocol",
} | set(keyword.kwlist)
# 과제 신청서 추가 항목은 기본 과제 항목명과 겹치면 안 된다(웹 서식은 별도 이름공간).
RESERVED_KEYS = set(PROTOCOL_FIELDS) | BASE_RESERVED
FIELD_KEYS = {"key", "label", "type", "required", "help", "placeholder", "options", "show_when", "min", "max",
              "material", "prefill"}
ONLINE_KEYS = {"sections", "label", "per_member", "signer", "required", "intro", "pledge", "description"}
PROTOCOL_KEYS = {"sections", "description"}
PREFILL_SOURCES: dict[str, str] = {
    "user.full_name": "작성자 성명",
    "user.department": "작성자 소속",
    "user.email": "작성자 이메일",
    "pi.full_name": "연구책임자 성명",
    "protocol.title": "연구 제목",
    "protocol.protocol_no": "과제번호",
    "protocol.funding_source": "연구비 지원기관",
    "today": "작성일",
}
MAX_FIELDS = 200


class SchemaError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class FieldDef:
    key: str
    label: str
    type: str = "text"
    required: bool = False
    options: list[dict[str, str]] = field(default_factory=list)
    help: str = ""
    section: str = ""
    placeholder: str = ""
    min: float | None = None
    max: float | None = None
    show_when: dict[str, Any] | None = None
    material: bool = False
    prefill: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def data_show_when(self, prefix: str) -> str:
        """화면의 data-show-when 속성값(app.js가 해석). 기본 과제 항목을 참조하면 그 입력 이름을 그대로 쓴다."""
        if not self.show_when:
            return ""
        ref = self.show_when.get("field", "")
        name = ref if (prefix == "custom__" and ref in PROTOCOL_FIELDS) else f"{prefix}{ref}"
        eq = self.show_when.get("equals")
        if eq is None:
            return name
        if eq is True:
            eq = "yes"
        elif eq is False:
            eq = "no"
        return f"{name}={eq}"


@dataclass
class OnlineFormSpec:
    doc_type: str
    label: str
    name: str
    per_member: bool
    signer: str
    required: bool
    intro: str
    pledge: str
    sections: list[dict[str, Any]]
    definition_id: int
    version: int

    @property
    def fields(self) -> list[FieldDef]:
        return [f for s in self.sections for f in s["fields"]]


# ------------------------------------------------------------------ 검증
def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_schema(kind: str, schema: Any) -> list[str]:
    errors: list[str] = []
    if not KIND_RE.match(kind or ""):
        errors.append("양식 종류(kind)는 'protocol' 또는 'online:<영문 서류코드>' 형식이어야 합니다.")
    if not isinstance(schema, dict):
        return errors + ["스키마는 키-값 구조(YAML 매핑)여야 합니다."]
    online = (kind or "").startswith("online:")
    allowed_top = ONLINE_KEYS if online else PROTOCOL_KEYS
    unknown = sorted(set(schema) - allowed_top)
    if unknown:
        errors.append(f"알 수 없는 최상위 항목: {', '.join(unknown)} (사용 가능: {', '.join(sorted(allowed_top))})")
    if online:
        if not isinstance(schema.get("label"), str) or not schema.get("label", "").strip():
            errors.append("웹 서식에는 표시 이름(label)이 필요합니다.")
        for b in ("per_member", "required"):
            if b in schema and not isinstance(schema[b], bool):
                errors.append(f"{b}는 true 또는 false여야 합니다.")
        if schema.get("signer", "any") not in ("any", "pi"):
            errors.append("signer는 any(연구진 누구나) 또는 pi(연구책임자)여야 합니다.")
        if schema.get("per_member") and schema.get("signer") == "pi":
            errors.append("개인별 서식(per_member: true)에는 signer: pi를 함께 쓸 수 없습니다.")
        for k in ("intro", "pledge", "description"):
            if k in schema and not isinstance(schema[k], str):
                errors.append(f"{k}는 문자열이어야 합니다.")

    sections = schema.get("sections", [])
    if not isinstance(sections, list):
        return errors + ["sections는 목록이어야 합니다."]
    keys: dict[str, dict[str, Any]] = {}
    total = 0
    for si, sec in enumerate(sections, 1):
        if not isinstance(sec, dict):
            errors.append(f"{si}번째 구역 형식이 올바르지 않습니다(title, fields 필요).")
            continue
        title = sec.get("title")
        if not isinstance(title, str) or not title.strip():
            errors.append(f"{si}번째 구역에 제목(title)이 필요합니다.")
            title = f"{si}번째 구역"
        extra = sorted(set(sec) - {"title", "fields", "description"})
        if extra:
            errors.append(f"구역 '{title}': 알 수 없는 항목 {', '.join(extra)}")
        fields = sec.get("fields")
        if not isinstance(fields, list) or not fields:
            errors.append(f"구역 '{title}'에 항목(fields)이 없습니다.")
            continue
        for fi, f in enumerate(fields, 1):
            total += 1
            where = f"구역 '{title}' {fi}번째 항목"
            if not isinstance(f, dict):
                errors.append(f"{where}: 형식이 올바르지 않습니다.")
                continue
            key = f.get("key")
            if isinstance(key, str) and KEY_RE.match(key):
                where = f"항목 '{key}'"
                if key in (BASE_RESERVED if online else RESERVED_KEYS):
                    errors.append(f"{where}: 예약어(기본 과제 항목명 등)라 key로 쓸 수 없습니다.")
                elif key in keys:
                    errors.append(f"{where}: key가 중복됩니다.")
                else:
                    keys[key] = f
            else:
                errors.append(f"{where}: key는 영문 소문자로 시작하는 영문 소문자·숫자·밑줄(40자 이하)이어야 합니다.")
            extra = sorted(set(f) - FIELD_KEYS)
            if extra:
                errors.append(f"{where}: 알 수 없는 속성 {', '.join(extra)}")
            label = f.get("label")
            if not isinstance(label, str) or not label.strip():
                errors.append(f"{where}: 표시 이름(label)이 필요합니다.")
            elif len(label) > 200:
                errors.append(f"{where}: label은 200자 이하여야 합니다.")
            ftype = f.get("type", "text")
            if ftype not in FIELD_TYPES:
                errors.append(f"{where}: 지원하지 않는 형식 '{ftype}' (사용 가능: {', '.join(FIELD_TYPES)})")
            opts = f.get("options")
            if ftype in ("select", "multiselect"):
                if not isinstance(opts, list) or not opts:
                    errors.append(f"{where}: 선택형 항목에는 options 목록이 필요합니다.")
                else:
                    values = []
                    for o in opts:
                        if isinstance(o, dict) and "value" in o:
                            values.append(str(o["value"]))
                        elif isinstance(o, (str, int, float)) and not isinstance(o, bool):
                            values.append(str(o))
                        else:
                            errors.append(f"{where}: options 항목은 문자열 또는 {{value, label}}이어야 합니다.")
                    if len(values) != len(set(values)):
                        errors.append(f"{where}: options 값이 중복됩니다.")
            elif opts is not None:
                errors.append(f"{where}: options는 선택형(select/multiselect)에만 사용합니다.")
            for b in ("required", "material"):
                if b in f and not isinstance(f[b], bool):
                    errors.append(f"{where}: {b}는 true 또는 false여야 합니다.")
            for s_ in ("help", "placeholder"):
                if s_ in f and not isinstance(f[s_], str):
                    errors.append(f"{where}: {s_}는 문자열이어야 합니다.")
            if "min" in f or "max" in f:
                if ftype != "number":
                    errors.append(f"{where}: min/max는 숫자(number) 항목에만 사용합니다.")
                elif any(k in f and not _is_num(f[k]) for k in ("min", "max")):
                    errors.append(f"{where}: min/max는 숫자여야 합니다.")
                elif _is_num(f.get("min")) and _is_num(f.get("max")) and f["min"] > f["max"]:
                    errors.append(f"{where}: min이 max보다 큽니다.")
            if "prefill" in f:
                if not online:
                    errors.append(f"{where}: prefill은 웹 서식에서만 사용할 수 있습니다.")
                elif f["prefill"] not in PREFILL_SOURCES:
                    errors.append(f"{where}: prefill 값은 {', '.join(PREFILL_SOURCES)} 중 하나여야 합니다.")
            sw = f.get("show_when")
            if sw is not None and (not isinstance(sw, dict) or not isinstance(sw.get("field"), str)
                                   or set(sw) - {"field", "equals"}):
                errors.append(f"{where}: show_when은 {{field: 항목키, equals: 값}} 형식이어야 합니다.")
    if total > MAX_FIELDS:
        errors.append(f"항목은 최대 {MAX_FIELDS}개까지 정의할 수 있습니다.")
    for key, f in keys.items():
        sw = f.get("show_when")
        if isinstance(sw, dict) and isinstance(sw.get("field"), str):
            ref = sw["field"]
            if ref == key:
                errors.append(f"항목 '{key}': show_when이 자기 자신을 참조합니다.")
            elif ref not in keys and (online or ref not in PROTOCOL_FIELDS):
                errors.append(f"항목 '{key}': show_when이 존재하지 않는 항목 '{ref}'을 참조합니다.")
    if online and not keys and not errors:
        errors.append("웹 서식에는 항목이 1개 이상 필요합니다.")
    return errors


def _norm_options(opts: Any) -> list[dict[str, str]]:
    out = []
    for o in opts or []:
        if isinstance(o, dict):
            out.append({"value": str(o["value"]), "label": str(o.get("label", o["value"]))})
        else:
            out.append({"value": str(o), "label": str(o)})
    return out


def parse_sections(schema: dict[str, Any]) -> list[dict[str, Any]]:
    sections = []
    for sec in (schema or {}).get("sections", []) or []:
        title = str(sec.get("title", ""))
        fields = []
        for f in sec.get("fields", []) or []:
            fields.append(FieldDef(
                key=f["key"], label=str(f.get("label", f["key"])), type=f.get("type", "text"),
                required=bool(f.get("required", False)), options=_norm_options(f.get("options")),
                help=str(f.get("help", "")), section=title, placeholder=str(f.get("placeholder", "")),
                min=f.get("min"), max=f.get("max"), show_when=f.get("show_when"),
                material=bool(f.get("material", False)), prefill=str(f.get("prefill", "")),
            ))
        sections.append({"title": title, "description": str(sec.get("description", "")), "fields": fields})
    return sections


def fields_of(schema: dict[str, Any]) -> list[FieldDef]:
    return [f for s in parse_sections(schema) for f in s["fields"]]


# ------------------------------------------------------------------ 조회
def get_active(db: Session, kind: str) -> FormDefinition | None:
    return db.scalars(
        select(FormDefinition).where(FormDefinition.kind == kind, FormDefinition.active.is_(True))
        .order_by(FormDefinition.version.desc())
    ).first()


def active_protocol_sections(db: Session) -> list[dict[str, Any]]:
    d = get_active(db, "protocol")
    return parse_sections(d.schema) if d else []


def active_protocol_fields(db: Session) -> list[FieldDef]:
    """활성 과제 신청서 추가 항목(인터페이스 계약)."""
    return [f for s in active_protocol_sections(db) for f in s["fields"]]


def custom_field_meta(db: Session) -> dict[str, dict[str, Any]]:
    """스냅샷에 함께 보관할 항목 메타(라벨·선택지). 양식이 바뀌어도 과거 제출본을 그대로 해석하기 위함."""
    return {
        f.key: {"label": f.label, "type": f.type, "options": option_map(f), "material": f.material}
        for f in active_protocol_fields(db)
    }


def online_spec(d: FormDefinition) -> OnlineFormSpec:
    s = d.schema or {}
    return OnlineFormSpec(
        doc_type=d.doc_type, label=str(s.get("label") or d.name), name=d.name,
        per_member=bool(s.get("per_member", False)), signer=str(s.get("signer", "any")),
        required=bool(s.get("required", False)), intro=str(s.get("intro", "")), pledge=str(s.get("pledge", "")),
        sections=parse_sections(s), definition_id=d.id, version=d.version,
    )


def active_online_forms(db: Session) -> list[OnlineFormSpec]:
    rows = db.scalars(
        select(FormDefinition).where(FormDefinition.kind.like("online:%"), FormDefinition.active.is_(True))
        .order_by(FormDefinition.kind)
    ).all()
    return [online_spec(d) for d in rows]


def get_online_spec(db: Session, doc_type: str) -> OnlineFormSpec | None:
    d = get_active(db, f"online:{doc_type}")
    return online_spec(d) if d else None


def kind_label(kind: str, schema: dict[str, Any] | None = None) -> str:
    if kind == "protocol":
        return "과제 신청서 기관 추가 항목"
    label = (schema or {}).get("label") or kind.split(":", 1)[-1]
    return f"웹 작성 서식 · {label}"


def has_any_definition(db: Session) -> bool:
    return bool(db.scalar(select(func.count(FormDefinition.id))))


# ------------------------------------------------------------------ 저장·활성화
def save_definition(
    db: Session,
    user: User | None,
    kind: str,
    name: str,
    schema: dict[str, Any],
    *,
    activate: bool = False,
    note: str = "",
    source: str = "",
    ip: str = "",
) -> FormDefinition:
    errors = validate_schema(kind, schema)
    if not (name or "").strip():
        errors.insert(0, "양식 이름을 입력하십시오.")
    if errors:
        raise SchemaError(errors)
    version = 1 + (db.scalar(select(func.max(FormDefinition.version)).where(FormDefinition.kind == kind)) or 0)
    d = FormDefinition(kind=kind, name=name.strip()[:200], version=version, schema=schema, active=False,
                       note=note.strip(), source=source[:100], created_by=user.id if user else None)
    db.add(d)
    db.flush()
    audit.record(db, user, "form_save", "form_definition", d.id, {"kind": kind, "version": version}, ip)
    if activate:
        activate_definition(db, user, d, ip)
    return d


def activate_definition(db: Session, user: User | None, d: FormDefinition, ip: str = "") -> None:
    for other in db.scalars(select(FormDefinition).where(FormDefinition.kind == d.kind, FormDefinition.active.is_(True))):
        other.active = False
    d.active = True
    db.flush()
    audit.record(db, user, "form_activate", "form_definition", d.id, {"kind": d.kind, "version": d.version}, ip)


def deactivate_definition(db: Session, user: User | None, d: FormDefinition, ip: str = "") -> None:
    d.active = False
    db.flush()
    audit.record(db, user, "form_deactivate", "form_definition", d.id, {"kind": d.kind, "version": d.version}, ip)


# ------------------------------------------------------------------ 폼 값 해석
def parse_form_values(form: Any, fields: list[FieldDef], prefix: str) -> tuple[dict[str, Any], dict[str, str]]:
    """HTML 폼 → {key: 값}. 비어 있는 값은 저장하지 않는다(체크박스 False는 저장)."""
    values: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for f in fields:
        raw = [str(v) for v in form.getlist(prefix + f.key)]
        value, err = coerce(f, raw)
        if err:
            errors[f.key] = err
        if value is None or value == "" or value == []:
            continue
        values[f.key] = value
    return values, errors
