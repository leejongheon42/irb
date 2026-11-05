"""가명처리 기법 (보건의료데이터 활용 가이드라인의 처리 기법 참고).

- 삭제, 가명(일방향 키 암호화/일련번호 대체), 마스킹
- 범주화(나이 구간, 날짜 연·월 단위, 주소 시군구 단위, 우편번호 앞자리)
- 날짜 이동(대상자별 동일 오프셋으로 간격 보존)
- 라운딩, 상·하단 코딩
- 자유기술 스크러빙
"""
from __future__ import annotations

import hashlib
import hmac
import re
from datetime import date, datetime, timedelta
from typing import Any

from . import patterns as P

ACTIONS: dict[str, str] = {
    "keep": "유지",
    "delete": "삭제",
    "pseudonymize_hmac": "가명(키 기반 일방향 암호화)",
    "serial": "가명(일련번호 대체, 매핑 분리보관)",
    "mask": "부분 마스킹",
    "date_shift": "날짜 이동(대상자별 동일 오프셋)",
    "date_generalize": "날짜 범주화(연/연월)",
    "birth_to_age_band": "생년월일 → 연령 구간",
    "age_band": "나이 → 연령 구간",
    "address_generalize": "주소 범주화(시도/시군구)",
    "zip_generalize": "우편번호 앞 3자리",
    "numeric_round": "수치 라운딩",
    "top_bottom_code": "상·하단 코딩",
    "text_scrub": "자유기술 개인정보 제거",
}

SIDO_NORMALIZE = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시",
    "광주": "광주광역시", "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시",
    "경기": "경기도", "강원": "강원특별자치도", "충북": "충청북도", "충남": "충청남도",
    "전북": "전북특별자치도", "전남": "전라남도", "경북": "경상북도", "경남": "경상남도",
    "제주": "제주특별자치도",
}

_DATE_FORMATS = ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


def parse_date(value: str) -> tuple[date | None, str | None]:
    v = value.strip()
    if not v:
        return None, None
    m = re.match(r"^(\d{4})년\s?(\d{1,2})월\s?(\d{1,2})일", v)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])), "%Y-%m-%d"
        except ValueError:
            return None, None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date(), fmt
        except ValueError:
            continue
    m = re.match(r"^(\d{4})[-./](\d{1,2})[-./](\d{1,2})", v)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])), "%Y-%m-%d"
        except ValueError:
            return None, None
    return None, None


class Pseudonymizer:
    """프로젝트별 솔트를 섞은 HMAC-SHA256 가명 생성기.

    같은 원본 값이라도 과제(프로젝트)가 다르면 다른 가명이 생성되어
    과제 간 무단 결합(연결)을 방지한다.
    """

    def __init__(self, secret: str, project_salt: str):
        self._key = hashlib.sha256(f"{secret}::{project_salt}".encode()).digest()

    def token(self, value: str, length: int = 16) -> str:
        normalized = re.sub(r"[\s-]", "", value.strip()).upper()
        return hmac.new(self._key, normalized.encode(), hashlib.sha256).hexdigest()[:length]

    def offset_days(self, subject_key: str, max_days: int) -> int:
        """대상자별 결정적 날짜 이동량(0 제외, -max~+max)."""
        if max_days <= 0:
            return 0
        h = int(self.token("DATESHIFT:" + subject_key, 12), 16)
        span = 2 * max_days
        off = (h % span) - max_days
        return off if off < 0 else off + 1


def mask(value: str, kind: str = "") -> str:
    v = value.strip()
    if not v:
        return v
    if kind == "name" or P.looks_like_korean_name(v):
        return v[0] + "*" * (len(v) - 1)
    digits = re.sub(r"\D", "", v)
    if kind == "phone" or P.MOBILE.fullmatch(v):
        return re.sub(r"\d(?=(?:\D*\d){4})", "*", v)
    if kind == "email" and "@" in v:
        local, domain = v.split("@", 1)
        return local[:1] + "***@" + domain
    if digits and len(digits) == len(re.sub(r"\D", "", v)) and len(digits) >= 6:
        return v[: len(v) // 3] + "*" * (len(v) - len(v) // 3)
    return v[0] + "*" * (len(v) - 1)


def age_band(age: int, width: int, top_code: int) -> str:
    if age >= top_code:
        return f"{top_code}+"
    lo = (age // width) * width
    return f"{lo}-{lo + width - 1}"


def age_at(birth: date, ref: date) -> int:
    return ref.year - birth.year - ((ref.month, ref.day) < (birth.month, birth.day))


def generalize_address(value: str, level: str = "sigungu") -> str:
    tokens = value.strip().split()
    if not tokens:
        return ""
    sido = tokens[0]
    for short, full in SIDO_NORMALIZE.items():
        if sido.startswith(short):
            sido = full
            break
    if level == "sido" or len(tokens) < 2:
        return sido
    second = tokens[1]
    if re.search(r"(시|군|구)$", second):
        # 'OO시 OO구' 형태(예: 성남시 분당구)는 구까지 유지
        if second.endswith("시") and len(tokens) > 2 and tokens[2].endswith("구"):
            return f"{sido} {second} {tokens[2]}"
        return f"{sido} {second}"
    return sido


def generalize_date(value: str, unit: str = "year_month") -> str:
    d, _ = parse_date(value)
    if d is None:
        return ""
    return f"{d.year}" if unit == "year" else f"{d.year}-{d.month:02d}"


def shift_date(value: str, days: int) -> str:
    d, fmt = parse_date(value)
    if d is None:
        return ""
    shifted = d + timedelta(days=days)
    return shifted.strftime(fmt if fmt and "%H" not in fmt else "%Y-%m-%d")


def round_numeric(value: str, base: float) -> str:
    try:
        x = float(value.replace(",", ""))
    except ValueError:
        return value
    if base <= 0:
        return value
    r = round(x / base) * base
    return str(int(r)) if float(base).is_integer() else f"{r:g}"


def top_bottom_code(value: str, lower: float | None, upper: float | None) -> str:
    try:
        x = float(value.replace(",", ""))
    except ValueError:
        return value
    if upper is not None and x >= upper:
        return f">={upper:g}"
    if lower is not None and x <= lower:
        return f"<={lower:g}"
    return value


def scrub_text(value: str, known_identifiers: list[str] | None = None) -> tuple[str, int]:
    """자유기술에서 패턴 기반 개인정보와 해당 행의 알려진 식별자(성명 등)를 제거."""
    count = 0
    out = value
    for ident in sorted({k for k in (known_identifiers or []) if k and len(k) >= 2}, key=len, reverse=True):
        if ident in out:
            count += out.count(ident)
            out = out.replace(ident, "[식별정보]")
    for _, pat, repl in P.TEXT_SCRUB_PATTERNS:
        out, n = pat.subn(repl, out)
        count += n
    # "홍길동 환자", "홍길동님" 형태의 이름 추정 제거
    name_hits = 0

    def _name(m: re.Match[str]) -> str:
        nonlocal name_hits
        word = m[1]
        if word not in _NAME_STOPWORDS and P.looks_like_korean_name(word):
            name_hits += 1
            return "[이름]" + m[2] + m[3]
        return m[0]

    out = _HONORIFIC_NAME.sub(_name, out)
    return out, count + name_hits


# 호칭(님/씨/환자) 앞의 2~4자 한글 이름. 호칭 뒤에는 조사(이/가/께서 등)가 올 수 있다.
_HONORIFIC_NAME = re.compile(r"(?<![가-힣])([가-힣]{2,4})(\s?)(님|환자|씨)")
_NAME_STOPWORDS = {
    "선생", "교수", "원장", "과장", "부장", "간호사", "의사", "보호자", "고혈압", "고지혈증", "정상",
    "신규", "기존", "소아", "성인", "노인", "외래", "입원", "응급", "중환자", "수술", "임산부",
    "남자", "여자", "남성", "여성", "고령", "전원", "재원", "퇴원", "사망", "해당", "동일", "상기",
    "고위험", "저위험", "중증", "경증", "만성", "급성", "장기", "신장", "간암", "위암", "폐암",
}


def describe(config: dict[str, Any]) -> str:
    action = config.get("action", "keep")
    label = ACTIONS.get(action, action)
    params = config.get("params") or {}
    if action == "date_generalize":
        label += f" ({'연' if params.get('unit') == 'year' else '연월'})"
    elif action == "address_generalize":
        label += f" ({'시도' if params.get('level') == 'sido' else '시군구'})"
    elif action == "numeric_round":
        label += f" (단위 {params.get('base', 1)})"
    elif action == "top_bottom_code":
        label += f" (하한 {params.get('lower', '-')}, 상한 {params.get('upper', '-')})"
    return label
