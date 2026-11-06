"""DOCX/HWPX 공통 처리 엔진.

문단 하나의 텍스트는 서식(run) 단위로 여러 조각(segment)에 나뉘어 저장된다.
워드·한글은 입력 도중 자리표시자를 ``{{연구`` + ``제목}}`` 처럼 쪼개 저장하는 일이 잦으므로,
문단 단위로 조각을 이어 붙여 자리표시자를 찾은 뒤 **첫 조각(=첫 run)의 서식을 유지**한 채 값을 기록한다.

반복 규칙
- 표의 한 행에 ``{{#목록}}`` 표식이 있으면 그 행을 목록 항목 수만큼 복제한다(빈 목록이면 빈 행 1개).
- 표 밖 문단에 ``{{#목록}}`` 이 있으면 그 문단(또는 ``{{/목록}}`` 이 있는 문단까지의 블록)을 복제한다.
- 반복 단위 안에서는 ``{{item}}``, ``{{item.속성}}``, ``{{no}}`` 를 쓸 수 있다.
"""
from __future__ import annotations

import copy
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import placeholders as PH


@dataclass
class Segment:
    """문단을 이루는 텍스트 조각(요소의 text 또는 tail)."""

    elem: Any
    attr: str  # "text" | "tail"

    def get(self) -> str:
        return getattr(self.elem, self.attr) or ""

    def set(self, value: str) -> None:
        setattr(self.elem, self.attr, value)


@dataclass
class Scope:
    """반복 행/문단 안의 현재 항목."""

    list_name: str
    item: Any = None
    index: int = 0
    empty: bool = False


Render = Callable[[PH.Placeholder, "Scope | None"], "str | None"]


@dataclass
class Dialect:
    """XML 방언(DOCX/HWPX)별 태그와 조각 추출 규칙."""

    p_tag: str
    tr_tag: str
    segments: Callable[[Any], list[Segment]]
    on_modified: Callable[[Segment], None] = lambda seg: None
    finalize: Callable[[Any], None] = lambda root: None


@dataclass
class FillStats:
    replaced: int = 0
    repeated_rows: int = 0
    repeated_blocks: int = 0
    warnings: list[str] = field(default_factory=list)


def owner(elem: Any, p_tag: str) -> Any:
    """가장 가까운 상위 문단 요소."""
    node = elem.getparent()
    while node is not None and node.tag != p_tag:
        node = node.getparent()
    return node


def is_attached(elem: Any, root: Any) -> bool:
    node = elem
    while node is not None:
        if node is root:
            return True
        node = node.getparent()
    return False


def inside(elem: Any, tag: str, root: Any) -> bool:
    node = elem.getparent()
    while node is not None and node is not root:
        if node.tag == tag:
            return True
        node = node.getparent()
    return False


def paragraph_text(p: Any, dialect: Dialect) -> str:
    return "".join(s.get() for s in dialect.segments(p))


# ---------------------------------------------------------------- 치환
def replace_in_paragraph(p: Any, dialect: Dialect, render: Render, scope: Scope | None) -> int:
    segs = dialect.segments(p)
    if not segs:
        return 0
    original = [s.get() for s in segs]
    combined = "".join(original)
    if "{{" not in combined and "«" not in combined and "[[" not in combined:
        return 0
    matches = PH.find(combined)
    if not matches:
        return 0
    starts: list[int] = []
    pos = 0
    for t in original:
        starts.append(pos)
        pos += len(t)

    def seg_at(i: int) -> int:
        return bisect_right(starts, i) - 1

    texts = list(original)
    count = 0
    # 뒤에서부터 치환하면 앞쪽 자리표시자의 위치가 변하지 않는다.
    for start, end, ph in reversed(matches):
        value = render(ph, scope)
        if value is None:
            continue
        si, ei = seg_at(start), seg_at(end - 1)
        s_off, e_off = start - starts[si], end - starts[ei]
        if si == ei:
            texts[si] = texts[si][:s_off] + value + texts[si][e_off:]
        else:
            texts[si] = texts[si][:s_off] + value
            for k in range(si + 1, ei):
                texts[k] = ""
            texts[ei] = texts[ei][e_off:]
        count += 1
    for seg, old, new in zip(segs, original, texts):
        if old != new:
            seg.set(new)
            dialect.on_modified(seg)
    return count


def replace_all(root: Any, dialect: Dialect, render: Render, stats: FillStats) -> None:
    for p in list(root.iter(dialect.p_tag)):
        stats.replaced += replace_in_paragraph(p, dialect, render, None)
    dialect.finalize(root)


# ---------------------------------------------------------------- 반복
def _first_block_marker(text: str) -> str | None:
    for _s, _e, ph in PH.find(text):
        if ph.marker == "#":
            return ph.name
    return None


def _has_end_marker(text: str, name: str) -> bool:
    return any(ph.marker == "/" and ph.name == name for _s, _e, ph in PH.find(text))


def _clone_for_items(
    template: list[Any],
    name: str,
    items: list[Any],
    dialect: Dialect,
    render: Render,
    stats: FillStats,
) -> list[Any]:
    clones: list[Any] = []
    units = list(enumerate(items, start=1)) if items else [(0, None)]
    for index, item in units:
        scope = Scope(list_name=name, item=item, index=index, empty=not items)
        for elem in template:
            clone = copy.deepcopy(elem)
            # iter()는 요소 자신도 포함하므로 문단이든 표 행이든 내부의 모든 문단을 처리한다.
            for p in list(clone.iter(dialect.p_tag)):
                stats.replaced += replace_in_paragraph(p, dialect, render, scope)
            clones.append(clone)
    return clones


def expand_repeats(
    root: Any,
    dialect: Dialect,
    render: Render,
    list_items: Callable[[str], "list[Any] | None"],
    stats: FillStats,
) -> None:
    # 1) 표 행 반복
    for tr in list(root.iter(dialect.tr_tag)):
        if not is_attached(tr, root):
            continue
        text = "".join(paragraph_text(p, dialect) for p in tr.iter(dialect.p_tag))
        name = _first_block_marker(text)
        if not name:
            continue
        items = list_items(name)
        if items is None:
            stats.warnings.append(f"반복 표식 '#{name}'에 연결된 목록 필드가 없습니다.")
            items = []
        parent = tr.getparent()
        idx = parent.index(tr)
        clones = _clone_for_items([tr], name, items, dialect, render, stats)
        parent.remove(tr)
        for k, clone in enumerate(clones):
            parent.insert(idx + k, clone)
        stats.repeated_rows += len(items)

    # 2) 표 밖 문단(블록) 반복
    for p in list(root.iter(dialect.p_tag)):
        if not is_attached(p, root) or inside(p, dialect.tr_tag, root):
            continue
        text = paragraph_text(p, dialect)
        name = _first_block_marker(text)
        if not name:
            continue
        parent = p.getparent()
        siblings = list(parent)
        start = siblings.index(p)
        block = [p]
        if not _has_end_marker(text, name):
            # 같은 부모 안에서 종료 표식({{/목록}})이 있는 문단까지를 하나의 반복 블록으로 본다.
            for sib in siblings[start + 1:]:
                if sib.tag != dialect.p_tag:
                    break
                block.append(sib)
                if _has_end_marker(paragraph_text(sib, dialect), name):
                    break
            if not _has_end_marker(paragraph_text(block[-1], dialect), name):
                block = [p]  # 종료 표식이 없으면 표식이 있는 문단만 반복
        items = list_items(name)
        if items is None:
            stats.warnings.append(f"반복 표식 '#{name}'에 연결된 목록 필드가 없습니다.")
            items = []
        clones = _clone_for_items(block, name, items, dialect, render, stats)
        for elem in block:
            parent.remove(elem)
        for k, clone in enumerate(clones):
            parent.insert(start + k, clone)
        stats.repeated_blocks += len(items)


# ---------------------------------------------------------------- 탐지
def detect(root: Any, dialect: Dialect) -> list[PH.Placeholder]:
    found: list[PH.Placeholder] = []
    for p in root.iter(dialect.p_tag):
        found.extend(ph for _s, _e, ph in PH.find(paragraph_text(p, dialect)))
    return found
