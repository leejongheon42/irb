"""Locate paragraph-sized evidence without accepting a whole document."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Section:
    title: str
    body: str
    start: int
    end: int


def split_sections(text: str) -> list[Section]:
    """Keep offsets and group a short standalone heading with its paragraph."""
    sections: list[Section] = []
    heading = ""
    body: list[str] = []
    start = 0
    offset = 0
    # 추출기가 제목 스타일을 "# "로 표시한 문서는 그 표시만 신뢰한다. 짧은 명사형 문장을
    # 제목으로 추측하면 바로 위 항목의 본문이 잘려 '내용 미흡'으로 잘못 판정된다.
    marked = sum(1 for ln in text.splitlines() if ln.startswith("# ")) >= 3
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if marked:
            is_heading = stripped.startswith("# ")
        else:
            is_heading = bool(stripped) and len(stripped) <= 45 and " | " not in stripped and not re.search(
                r"[.!?。]$|(?:니다|한다|있다|없다|함|임|됨)$|[:：].+", stripped,
            )
        if is_heading:
            if heading:
                sections.append(Section(heading, "\n".join(body), start, offset))
            heading, start = stripped, offset
            body = []
        elif stripped:
            if heading:
                body.append(stripped)
            else:
                sections.append(Section("", stripped, offset, offset + len(line)))
        offset += len(line)
    if heading:
        sections.append(Section(heading, "\n".join(body), start, offset))
    return sections


def matches(text: str, pattern: str) -> bool:
    if pattern.startswith("re:"):
        return re.search(pattern[3:], text, re.I) is not None
    return pattern.casefold() in text.casefold()


def substantive(text: str, min_chars: int) -> bool:
    """A keyword list is not a narrative statement, even when it is long.

    A table (two or more rows) is written content even without a sentence ending.
    """
    if len(re.sub(r"\s", "", text)) < min_chars:
        return False
    if sum(1 for line in text.splitlines() if " | " in line) >= 2:
        return True
    return bool(re.search(
        r"(?:니다|한다|된다|있다|없다|이며|으며|하고|하여|위해)|[가-힣](?:다|요|오|함|임|됨|음)\s*(?:[.!?。]|$)", text, re.M,
    ))
