"""기관 프로필과 과제번호 형식.

과제번호 형식 토큰: {YYYY} 4자리 연도, {YY} 2자리 연도, {SEQ} 일련번호, {SEQ:n} n자리 0채움 일련번호.
예) "IRB-{YYYY}-{SEQ:4}" → IRB-2026-0001,  "{YY}-{SEQ:3}-C" → 26-001-C
일련번호는 같은 형식·같은 연도의 기존 번호 중 최댓값 + 1 이다(연도 토큰이 없으면 해마다 초기화되지 않음).
"""
from __future__ import annotations

import re
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..models import Protocol, User
from .models import DEFAULT_PROTOCOL_NO_FORMAT, InstitutionProfile

TOKEN_RE = re.compile(r"\{(YYYY|YY|SEQ(?::(\d{1,2}))?)\}")
_LITERAL_OK = re.compile(r"^[A-Za-z0-9\-_/.()가-힣 ]*$")
PROFILE_FIELDS = ("institution_name", "irb_name", "protocol_no_format", "letter_footer", "contact")
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_TTL = 30.0


# ------------------------------------------------------------------ 과제번호 형식
def format_protocol_no(fmt: str, year: int, seq: int) -> str:
    def repl(m: re.Match[str]) -> str:
        tok = m.group(1)
        if tok == "YYYY":
            return f"{year:04d}"
        if tok == "YY":
            return f"{year % 100:02d}"
        width = int(m.group(2) or 0)
        return f"{seq:0{width}d}" if width else str(seq)

    return TOKEN_RE.sub(repl, fmt)


def validate_format(fmt: str) -> list[str]:
    fmt = (fmt or "").strip()
    if not fmt:
        return ["과제번호 형식을 입력하십시오."]
    errors: list[str] = []
    seq_tokens = [m for m in TOKEN_RE.finditer(fmt) if m.group(1).startswith("SEQ")]
    if len(seq_tokens) != 1:
        errors.append("일련번호 토큰 {SEQ} 또는 {SEQ:n}을 정확히 1개 포함해야 합니다.")
    for m in seq_tokens:
        if m.group(2) and not 1 <= int(m.group(2)) <= 8:
            errors.append("일련번호 자릿수는 1~8 사이여야 합니다.")
    literal = TOKEN_RE.sub("", fmt)
    if "{" in literal or "}" in literal:
        errors.append("알 수 없는 토큰이 있습니다. 사용 가능: {YYYY} {YY} {SEQ} {SEQ:n}")
    elif not _LITERAL_OK.match(literal):
        errors.append("영문·숫자·한글과 - _ / . ( ) 공백만 사용할 수 있습니다.")
    if not errors and len(format_protocol_no(fmt, 2026, 99999)) > 32:
        errors.append("생성되는 과제번호가 32자를 넘습니다.")
    return errors


def protocol_no_regex(fmt: str, year: int) -> re.Pattern[str]:
    parts: list[str] = []
    pos = 0
    for m in TOKEN_RE.finditer(fmt):
        parts.append(re.escape(fmt[pos:m.start()]))
        tok = m.group(1)
        if tok == "YYYY":
            parts.append(f"{year:04d}")
        elif tok == "YY":
            parts.append(f"{year % 100:02d}")
        else:
            parts.append(r"(?P<seq>\d+)")
        pos = m.end()
    parts.append(re.escape(fmt[pos:]))
    return re.compile("^" + "".join(parts) + "$")


def _literal_prefix(fmt: str, year: int) -> str:
    """일련번호 토큰 앞부분(연도 치환) — DB 조회 범위를 좁히는 데 사용."""
    m = next((m for m in TOKEN_RE.finditer(fmt) if m.group(1).startswith("SEQ")), None)
    before = fmt[: m.start()] if m else fmt
    return format_protocol_no(before, year, 0)


def next_protocol_no(db: Session, year: int) -> str:
    fmt = get_profile(db).protocol_no_format or DEFAULT_PROTOCOL_NO_FORMAT
    if validate_format(fmt):
        fmt = DEFAULT_PROTOCOL_NO_FORMAT
    rx = protocol_no_regex(fmt, year)
    prefix = _literal_prefix(fmt, year)
    q = select(Protocol.protocol_no).where(Protocol.protocol_no.is_not(None))
    if prefix:
        q = q.where(Protocol.protocol_no.like(prefix.replace("%", r"\%").replace("_", r"\_") + "%", escape="\\"))
    existing = set(db.scalars(q))
    seq = 1 + max((int(m.group("seq")) for no in existing if (m := rx.match(no))), default=0)
    candidate = format_protocol_no(fmt, year, seq)
    taken = existing | set(db.scalars(select(Protocol.protocol_no).where(Protocol.protocol_no == candidate)))
    while candidate in taken:
        seq += 1
        candidate = format_protocol_no(fmt, year, seq)
    return candidate


# ------------------------------------------------------------------ 프로필
def get_profile(db: Session) -> InstitutionProfile:
    """기관 프로필(없으면 환경설정값으로 생성)."""
    p = db.scalars(select(InstitutionProfile).order_by(InstitutionProfile.id)).first()
    if p is None:
        s = get_settings()
        p = InstitutionProfile(institution_name=s.institution_name, irb_name=s.irb_name,
                               protocol_no_format=DEFAULT_PROTOCOL_NO_FORMAT)
        db.add(p)
        db.flush()
    return p


def _defaults() -> dict[str, Any]:
    s = get_settings()
    return {"institution_name": s.institution_name, "irb_name": s.irb_name,
            "protocol_no_format": DEFAULT_PROTOCOL_NO_FORMAT, "letter_footer": "", "contact": ""}


def _read_values(db: Session) -> dict[str, Any]:
    values = _defaults()
    row = db.scalars(select(InstitutionProfile).order_by(InstitutionProfile.id)).first()
    if row is not None:
        for f in PROFILE_FIELDS:
            v = getattr(row, f)
            if v:
                values[f] = v
    return values


def profile_values(db: Session | None = None) -> dict[str, Any]:
    """화면 표시용 프로필 값(읽기 전용, 30초 캐시). DB를 사용할 수 없으면 환경설정값."""
    key = get_settings().database_url
    hit = _CACHE.get(key)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    try:
        if db is not None:
            values = _read_values(db)
        else:
            from ..database import SessionLocal

            with SessionLocal() as own:
                values = _read_values(own)
    except SQLAlchemyError:
        return _defaults()
    _CACHE[key] = (time.monotonic(), values)
    return values


def invalidate_cache() -> None:
    _CACHE.clear()


def update_profile(db: Session, user: User, data: dict[str, Any], ip: str = "") -> list[str]:
    errors: list[str] = []
    clean = {f: str(data.get(f, "") or "").strip() for f in PROFILE_FIELDS}
    if not clean["institution_name"]:
        errors.append("기관명을 입력하십시오.")
    if not clean["irb_name"]:
        errors.append("위원회 명칭을 입력하십시오.")
    errors += validate_format(clean["protocol_no_format"])
    for f, limit in (("institution_name", 200), ("irb_name", 200), ("letter_footer", 2000), ("contact", 1000)):
        if len(clean[f]) > limit:
            errors.append(f"{f} 길이가 너무 깁니다({limit}자 이하).")
    if errors:
        return errors
    p = get_profile(db)
    changed = {f: [getattr(p, f), v] for f, v in clean.items() if getattr(p, f) != v}
    for f, v in clean.items():
        setattr(p, f, v)
    p.updated_by = user.id
    if changed:
        audit.record(db, user, "profile_update", "institution_profile", p.id,
                     {f: c[1] for f, c in changed.items()}, ip)
    invalidate_cache()
    return []


class SettingsView:
    """템플릿용 설정 래퍼. 기관명·IRB명은 기관 프로필 값을 우선 사용하고, 나머지는 환경설정을 그대로 노출한다.

    템플릿을 고치지 않고도 `settings.institution_name`, `settings.irb_name`이 프로필을 따르며,
    추가로 `settings.letter_footer`, `settings.irb_contact`, `settings.protocol_no_format`을 쓸 수 있다.
    """

    def __init__(self, settings: Any, profile: dict[str, Any]):
        self._settings = settings
        self._profile = profile

    @property
    def institution_name(self) -> str:
        return self._profile.get("institution_name") or self._settings.institution_name

    @property
    def irb_name(self) -> str:
        return self._profile.get("irb_name") or self._settings.irb_name

    @property
    def letter_footer(self) -> str:
        return self._profile.get("letter_footer", "")

    @property
    def irb_contact(self) -> str:
        return self._profile.get("contact", "")

    @property
    def protocol_no_format(self) -> str:
        return self._profile.get("protocol_no_format", DEFAULT_PROTOCOL_NO_FORMAT)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._settings, name)
