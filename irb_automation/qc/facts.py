"""서류 본문에서 교차 대조할 핵심 값(버전·대상자 수·기간·용량·방문 횟수 등)을 추출한다.

추출 결과는 Fact(key, value, raw, line) 목록이다. value는 비교용으로 정규화한 값이고,
raw는 문서에 실제로 적힌 문자열(자동 교정 시 찾아 바꿀 대상)이다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

NUM = r"\d{1,3}(?:,\d{3})+|\d+"


@dataclass(frozen=True)
class Fact:
    key: str
    value: str
    raw: str
    line: str = ""


# ------------------------------------------------------------------ 버전
_VER_BODY = re.compile(
    r"(?:버전|버젼|Version|Ver\.?|VER\.?|개정\s*번호|판\s*번호)\s*[:：]?\s*[vV]?\s*(\d+(?:\.\d+){0,2})"
    r"|(?<![A-Za-z0-9])[vV]\s?(\d+\.\d+(?:\.\d+)?)(?![\d.])"
)
_VER_FILE = re.compile(r"(?:[vV](?:er(?:sion)?)?[ ._-]?|버전[ ._-]?)(\d+(?:[._]\d+){0,2})(?![\d])")


def normalize_version(v: str) -> str:
    parts = re.split(r"[._]", v.strip())
    while len(parts) > 1 and parts[-1] == "0":
        parts.pop()
    return ".".join(str(int(x)) for x in parts if x.isdigit()) or v


def version_from_filename(name: str) -> str | None:
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", name)
    found = list(_VER_FILE.finditer(stem))
    return normalize_version(found[-1].group(1)) if found else None


def versions_in_body(text: str) -> list[Fact]:
    head = text[:4000] + "\n" + text[-1500:]  # 표지·머리글·바닥글에 주로 표기
    out: list[Fact] = []
    for m in _VER_BODY.finditer(head):
        v = m.group(1) or m.group(2)
        if not v:
            continue
        out.append(Fact("version", normalize_version(v), m.group(0).strip(), _line_of(head, m.start())))
    return out


# ------------------------------------------------------------------ 날짜·기간
_DATE = r"(\d{4})\s*(?:[.\-/]|년)\s*(\d{1,2})\s*(?:[.\-/]|월)\s*(\d{1,2})\s*일?"
_RANGE = re.compile(_DATE + r"\s*\.?\s*(?:[~∼〜–—-]|부터|에서)\s*" + _DATE + r"\s*(?:까지)?")
_PERIOD_LINE = {
    "study_period": re.compile(r"연구\s*기간|연구\s*수행\s*기간|과제\s*기간"),
    "data_period": re.compile(r"자료\s*(?:수집|조사|추출)?\s*(?:대상)?\s*기간|조사\s*대상\s*기간|의무기록\s*검토\s*기간|데이터\s*기간"),
}


def parse_date(y: str, m: str, d: str) -> date | None:
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def invalid_dates(text: str) -> list[str]:
    out = []
    for m in re.finditer(_DATE, text):
        y, mo, d = m.groups()
        if 1900 <= int(y) <= 2100 and parse_date(y, mo, d) is None:
            out.append(m.group(0).strip())
    return out


def date_ranges(text: str) -> list[tuple[date | None, date | None, str, str]]:
    out = []
    for m in _RANGE.finditer(text):
        g = m.groups()
        out.append((parse_date(*g[0:3]), parse_date(*g[3:6]), m.group(0).strip(), _line_of(text, m.start())))
    return out


def _fmt_range(a: date | None, b: date | None) -> str:
    return f"{a.isoformat() if a else '?'}~{b.isoformat() if b else '?'}"


# ------------------------------------------------------------------ 수치 항목
_SUBJECTS = re.compile(
    r"(?:목표\s*)?(?:연구\s*)?(?:대상자|피험자|시험대상자|환자|참여자)\s*(?:수|의\s*수)?\s*(?:는|은|:|：)?\s*"
    r"(?:총|약|최대|전체)?\s*(" + NUM + r")\s*(?:명|례|건)"
    r"|(?:총|약)\s*(" + NUM + r")\s*명(?:의|이|을)?\s*(?:연구대상자|대상자|환자|피험자|참여)"
)
_DOSAGE = re.compile(
    r"(?:1일|하루|매일)\s*(\d+)\s*회\s*,?\s*(?:1회\s*)?(\d+(?:\.\d+)?)\s*(mg|㎎|g|mL|ml|㎖|μg|mcg|IU|단위)"
)
_VISITS = re.compile(r"(?:총|전체)\s*(\d+)\s*회\s*(?:의\s*)?(?:방문|내원)|(?:방문|내원)\s*(?:횟수)?\s*(?:는|은|:|：)?\s*(?:총)?\s*(\d+)\s*회")
_DURATION = re.compile(
    r"(?:시험|참여|투약|투여|치료|관찰)\s*기간\s*(?:은|는|:|：)?\s*(?:총|약)?\s*(\d+)\s*(주|개월|달|일|년)"
)
_RETENTION = re.compile(r"(\d+)\s*년\s*(?:간|동안)?\s*(?:보관|보존)")
_DRUG = re.compile(r"(?:시험약|연구약|시험약물|시험제품|임상시험용\s*의약품)\s*(?:명|명칭)?\s*(?:은|는|:|：)\s*([A-Za-z가-힣][A-Za-z0-9가-힣\-]{1,30})")
_PI = re.compile(r"(?:연구책임자|책임연구자|연구 책임자)\s*(?:성명|이름)?\s*(?:은|는|:|：)\s*([가-힣]{2,4})(?![가-힣])")
_TITLE = re.compile(r"(?:연구\s*제목|연구\s*과제명|과제명|연구명)\s*(?:\(국문\))?\s*[:：]\s*(.{6,200})")

UNIT_ALIASES = {"㎎": "mg", "㎖": "mL", "ml": "mL", "mcg": "μg", "달": "개월"}


def _num(s: str) -> int:
    return int(s.replace(",", ""))


def _line_of(text: str, pos: int) -> str:
    start = text.rfind("\n", 0, pos) + 1
    end = text.find("\n", pos)
    return text[start:end if end != -1 else None].strip()[:200]


def extract(text: str) -> list[Fact]:
    facts: list[Fact] = []
    for m in _SUBJECTS.finditer(text):
        n = m.group(1) or m.group(2)
        if n and 1 <= _num(n) <= 10_000_000:
            facts.append(Fact("subjects", str(_num(n)), m.group(0).strip(), _line_of(text, m.start())))
    for m in _DOSAGE.finditer(text):
        unit = UNIT_ALIASES.get(m.group(3), m.group(3))
        facts.append(Fact("dosage", f"{int(m.group(1))}회/{float(m.group(2)):g}{unit}", m.group(0).strip(),
                          _line_of(text, m.start())))
    for m in _VISITS.finditer(text):
        n = m.group(1) or m.group(2)
        facts.append(Fact("visits", str(int(n)), m.group(0).strip(), _line_of(text, m.start())))
    for m in _DURATION.finditer(text):
        unit = UNIT_ALIASES.get(m.group(2), m.group(2))
        facts.append(Fact("duration", f"{int(m.group(1))}{unit}", m.group(0).strip(), _line_of(text, m.start())))
    for m in _RETENTION.finditer(text):
        facts.append(Fact("retention", str(int(m.group(1))), m.group(0).strip(), _line_of(text, m.start())))
    for m in _DRUG.finditer(text):
        facts.append(Fact("drug", m.group(1).strip().lower(), m.group(1).strip(), _line_of(text, m.start())))
    for m in _PI.finditer(text):
        facts.append(Fact("pi", m.group(1), m.group(1), _line_of(text, m.start())))
    for m in _TITLE.finditer(text):
        title = m.group(1).strip().rstrip(".")
        facts.append(Fact("title", _norm_title(title), title, _line_of(text, m.start())))
    for a, b, raw, line in date_ranges(text):
        for key, pat in _PERIOD_LINE.items():
            if pat.search(line):
                facts.append(Fact(key, _fmt_range(a, b), raw, line))
                break
    return facts


def _norm_title(t: str) -> str:
    return re.sub(r"[\s\W_]+", "", t).lower()


# ------------------------------------------------------------------ 과제 입력값 → 비교 기준
def record_facts(protocol) -> list[Fact]:
    """시스템에 입력된 과제 정보를 같은 형식의 Fact로 만든다(교차 대조의 기준값)."""
    out: list[Fact] = []
    if getattr(protocol, "expected_subjects", 0):
        n = int(protocol.expected_subjects)
        out.append(Fact("subjects", str(n), f"{n:,}명"))
    if protocol.study_start or protocol.study_end:
        out.append(Fact("study_period", _fmt_range(protocol.study_start, protocol.study_end),
                        _kr_range(protocol.study_start, protocol.study_end)))
    if protocol.data_period_start or protocol.data_period_end:
        out.append(Fact("data_period", _fmt_range(protocol.data_period_start, protocol.data_period_end),
                        _kr_range(protocol.data_period_start, protocol.data_period_end)))
    if getattr(protocol, "retention_years", None):
        out.append(Fact("retention", str(int(protocol.retention_years)), f"{int(protocol.retention_years)}년"))
    pi = getattr(protocol, "pi", None)
    if pi is not None and pi.full_name:
        out.append(Fact("pi", pi.full_name, pi.full_name))
    if protocol.title:
        out.append(Fact("title", _norm_title(protocol.title), protocol.title))
    return out


def _kr_range(a: date | None, b: date | None) -> str:
    def f(d: date | None) -> str:
        return f"{d.year}. {d.month:02d}. {d.day:02d}." if d else ""

    return f"{f(a)} ~ {f(b)}"


def restyle(key: str, old_raw: str, ref: Fact) -> str | None:
    """old_raw(문서 표기)를 기준값 ref에 맞게 고친 문자열. 형식은 가능한 한 원문을 따른다."""
    if key in ("subjects", "visits", "retention"):
        nums = list(re.finditer(NUM, old_raw))
        if not nums:
            return None
        m = nums[0] if key != "subjects" else max(nums, key=lambda x: _num(x.group(0)))
        target = int(ref.value)
        text = f"{target:,}" if "," in m.group(0) or (key == "subjects" and target >= 1000) else str(target)
        return old_raw[:m.start()] + text + old_raw[m.end():]
    if key in ("study_period", "data_period"):
        a, b = (ref.value.split("~") + ["?"])[:2]
        if "?" in (a, b):
            return None
        olds = list(re.finditer(_DATE, old_raw))
        if len(olds) < 2:
            return ref.raw
        da, db_ = date.fromisoformat(a), date.fromisoformat(b)
        out = old_raw
        for m, d in reversed(list(zip(olds[:2], (da, db_)))):
            core = m.group(0).rstrip()
            out = out[:m.start()] + _like(core, d) + m.group(0)[len(core):] + out[m.end():]
        return out
    if key in ("dosage", "duration", "drug", "pi", "title"):
        return ref.raw if ref.raw and ref.raw != old_raw else None
    return None


def _like(sample: str, d: date) -> str:
    """표기 형식(2024.01.05 / 2024-01-05 / 2024년 1월 5일)을 유지해 날짜를 바꾼다."""
    if "년" in sample:
        pad = bool(re.search(r"년\s*0\d", sample))
        mm = f"{d.month:02d}" if pad else str(d.month)
        dd = f"{d.day:02d}" if pad else str(d.day)
        sp = " " if re.search(r"년\s", sample) else ""
        return f"{d.year}년{sp}{mm}월{sp}{dd}일"
    sep = re.search(r"\d{4}\s*([.\-/])", sample)
    s = sep.group(1) if sep else "."
    spaced = bool(re.search(r"\d{4}\s*[.\-/]\s", sample))
    j = f"{s} " if spaced else s
    return f"{d.year}{j}{d.month:02d}{j}{d.day:02d}" + ("." if sample.rstrip().endswith(".") and s == "." else "")
