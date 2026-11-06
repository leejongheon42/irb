"""양식 필드 공통 로직(순수 함수, DB 의존 없음).

사전검토 엔진(engine/findings.py)과 화면이 같은 규칙으로 필수 여부·표시 조건·값 변환을 판단하도록 한 곳에 둔다.
"""
from __future__ import annotations

from datetime import date
from typing import Any

FIELD_TYPES: dict[str, str] = {
    "text": "한 줄 텍스트",
    "textarea": "여러 줄 텍스트",
    "number": "숫자",
    "date": "날짜",
    "select": "단일 선택",
    "multiselect": "복수 선택",
    "checkbox": "체크박스(동의/확인)",
    "yesno": "예/아니오",
}
MAX_TEXT = 5000


def _f(field: Any, name: str, default: Any = None) -> Any:
    """FieldDef(dataclass) 또는 dict 모두에서 속성을 읽는다."""
    if isinstance(field, dict):
        return field.get(name, default)
    return getattr(field, name, default)


def option_map(field: Any) -> dict[str, str]:
    return {str(o["value"]): str(o.get("label", o["value"])) for o in (_f(field, "options") or [])}


def is_empty(value: Any, ftype: str) -> bool:
    if ftype == "checkbox":
        return not value
    if ftype == "yesno":
        return value is None
    return value is None or value == "" or value == [] or value == {}


def _norm(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "" if value is None else str(value)


def is_visible(field: Any, custom: dict[str, Any], core: dict[str, Any] | None = None) -> bool:
    """show_when 조건: {field: 다른 항목 키 또는 기본 과제 항목명, equals: 값(생략 시 '값이 있으면')}."""
    sw = _f(field, "show_when")
    if not sw:
        return True
    ref = sw.get("field")
    if ref in (custom or {}):
        value = custom.get(ref)
    else:
        value = (core or {}).get(ref)
    if sw.get("equals") is None:
        return bool(value)
    expected = _norm(sw.get("equals"))
    if isinstance(value, (list, tuple, set)):
        return expected in {str(v) for v in value}
    return _norm(value) == expected


def display(field: Any, value: Any) -> str:
    ftype = _f(field, "type", "text")
    if value is None or value == "" or value == []:
        return "-"
    if ftype in ("checkbox", "yesno") or isinstance(value, bool):
        return "예" if value else "아니오"
    opts = option_map(field)
    if isinstance(value, list):
        return ", ".join(opts.get(str(v), str(v)) for v in value)
    if opts:
        return opts.get(str(value), str(value))
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def coerce(field: Any, raw: list[str]) -> tuple[Any, str | None]:
    """폼 원시값 → 저장값. (값, 오류메시지) 반환. 허용되지 않은 값은 버린다."""
    ftype = _f(field, "type", "text")
    first = str(raw[0]).strip() if raw else ""
    if ftype == "checkbox":
        return (first.lower() in ("on", "true", "1", "yes")), None
    if ftype == "yesno":
        if first == "yes":
            return True, None
        if first == "no":
            return False, None
        return None, None
    if ftype == "multiselect":
        allowed = option_map(field)
        values = [str(v) for v in raw if str(v) in allowed]
        return list(dict.fromkeys(values)), None
    if ftype == "select":
        if not first:
            return None, None
        if first not in option_map(field):
            return None, "허용되지 않은 선택값입니다."
        return first, None
    if ftype == "number":
        if not first:
            return None, None
        try:
            num = float(first.replace(",", ""))
        except ValueError:
            return None, "숫자를 입력하십시오."
        return (int(num) if num.is_integer() else num), None
    if ftype == "date":
        if not first:
            return None, None
        try:
            return date.fromisoformat(first).isoformat(), None
        except ValueError:
            return None, "날짜 형식(YYYY-MM-DD)이 아닙니다."
    return first[:MAX_TEXT], None


def range_error(field: Any, value: Any) -> str | None:
    if _f(field, "type") != "number" or not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    lo, hi = _f(field, "min"), _f(field, "max")
    if lo is not None and value < lo:
        return f"{lo:g} 이상이어야 합니다."
    if hi is not None and value > hi:
        return f"{hi:g} 이하여야 합니다."
    return None


def validate_values(fields: list[Any], values: dict[str, Any], core: dict[str, Any] | None = None) -> dict[str, str]:
    """필수 누락·범위 오류 → {key: 메시지}. 표시 조건이 거짓인 항목은 검사하지 않는다."""
    errors: dict[str, str] = {}
    for f in fields:
        key = _f(f, "key")
        if not is_visible(f, values, core):
            continue
        value = values.get(key)
        if _f(f, "required") and is_empty(value, _f(f, "type", "text")):
            errors[key] = "필수 항목입니다."
            continue
        err = range_error(f, value)
        if err:
            errors[key] = err
    return errors
