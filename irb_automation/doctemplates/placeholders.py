"""자리표시자 문법.

지원 구분자: ``{{필드}}``, ``«필드»``(워드 메일머지 표시), ``[[필드]]``

구분자 안의 문법::

    [#|/]이름[(=|~)값][|형식]

- ``{{연구제목}}``               이름 → 매핑된 과제 필드 값
- ``{{작성일|YYYY년 MM월 DD일}}``  날짜 형식, ``{{수집항목|줄바꿈}}`` 목록을 줄바꿈으로
- ``{{동의방식=waiver}}``        값 비교 → ■/□ (목록 필드는 포함 여부, ``~``는 부분 일치)
- ``{{#연구진}}``                표 행(또는 문단) 반복 시작 표식, ``{{/연구진}}`` 종료 표식(생략 가능)
- 반복 행 안에서는 ``{{item}}``, ``{{item.name}}``, ``{{no}}``(순번)를 쓴다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

PLACEHOLDER_RE = re.compile(
    r"\{\{(?P<a>[^{}\n]{1,200}?)\}\}|«(?P<b>[^«»\n]{1,200}?)»|\[\[(?P<c>[^\[\]\n]{1,200}?)\]\]"
)

# 반복 행/문단 안에서만 의미가 있는 이름(매핑 대상 아님)
ITEM_ALIASES = {
    "item": "item",
    "항목": "item",
    "no": "no",
    "순번": "no",
    "번호": "no",
}
ITEM_ATTR_ALIASES = {
    "성명": "name",
    "이름": "name",
    "항목명": "name",
    "소속": "department",
    "부서": "department",
    "역할": "role",
    "구분": "role",
    "이메일": "email",
    "연락처": "email",
    "교육만료일": "training_expires_on",
    "교육이수": "training_expires_on",
    "분류": "category",
    "유형": "type",
    "처리방법": "handling",
    "처리": "handling",
}


@dataclass(frozen=True)
class Placeholder:
    raw: str        # 구분자를 포함한 원문
    marker: str     # "" | "#" | "/"
    name: str       # 이름(매핑 키)
    op: str         # "" | "=" | "~"
    operand: str    # 비교 값
    fmt: str        # 형식 지정

    @property
    def kind(self) -> str:
        if self.marker:
            return "list"
        if self.op:
            return "compare"
        return "field"

    @property
    def is_item_scoped(self) -> bool:
        return self.name.split(".", 1)[0] in ITEM_ALIASES


def parse(inner: str, raw: str) -> Placeholder | None:
    text = inner.strip()
    if not text:
        return None
    marker = ""
    if text[0] in "#/":
        marker, text = text[0], text[1:].strip()
    fmt = ""
    if "|" in text:
        text, fmt = (part.strip() for part in text.split("|", 1))
    op = operand = ""
    for symbol in ("=", "~"):
        if symbol in text:
            text, operand = (part.strip() for part in text.split(symbol, 1))
            op = symbol
            break
    name = text.strip()
    if not name or len(name) > 100:
        return None
    return Placeholder(raw=raw, marker=marker, name=name, op=op, operand=operand, fmt=fmt)


def find(text: str) -> list[tuple[int, int, Placeholder]]:
    """문자열에서 자리표시자 위치를 찾는다. [(start, end, Placeholder)]"""
    out = []
    for m in PLACEHOLDER_RE.finditer(text):
        inner = m.group("a") or m.group("b") or m.group("c") or ""
        ph = parse(inner, m.group(0))
        if ph is not None:
            out.append((m.start(), m.end(), ph))
    return out


def normalize_item_name(name: str) -> tuple[str, str]:
    """반복 행 안의 이름 → ('item'|'no', 속성)."""
    base, _, attr = name.partition(".")
    return ITEM_ALIASES.get(base, base), ITEM_ATTR_ALIASES.get(attr, attr)
