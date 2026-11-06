"""DOCX(워드) 서식 채우기.

본문·표(중첩 표 포함)·텍스트 상자·머리글·바닥글·각주의 자리표시자를 치환한다.
워드 편지 병합(MERGEFIELD) 필드는 먼저 일반 텍스트 «필드명» 으로 풀어 같은 규칙으로 처리하므로,
기관이 기존에 쓰던 메일머지 서식도 그대로 올릴 수 있다.
"""
from __future__ import annotations

import re
from io import BytesIO
from typing import Any

from docx import Document as load_document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from . import core
from . import placeholders as PH

W_P, W_T, W_TR, W_R = qn("w:p"), qn("w:t"), qn("w:tr"), qn("w:r")
W_FLDSIMPLE, W_FLDCHAR, W_INSTR = qn("w:fldSimple"), qn("w:fldChar"), qn("w:instrText")
XML_SPACE = qn("xml:space")

_MERGEFIELD = re.compile(r'MERGEFIELD\s+(?:"([^"]+)"|(\S+))', re.IGNORECASE)


def _segments(p: Any) -> list[core.Segment]:
    return [core.Segment(t, "text") for t in p.iter(W_T) if core.owner(t, W_P) is p]


def _preserve(seg: core.Segment) -> None:
    seg.elem.set(XML_SPACE, "preserve")


def _split_newlines(root: Any) -> None:
    """값에 들어 있는 줄바꿈을 워드 줄바꿈(w:br)으로 바꾼다."""
    for t in list(root.iter(W_T)):
        text = t.text or ""
        if "\n" not in text:
            continue
        run = t.getparent()
        lines = text.split("\n")
        t.text = lines[0]
        t.set(XML_SPACE, "preserve")
        pos = run.index(t)
        for line in lines[1:]:
            run.insert(pos + 1, OxmlElement("w:br"))
            new_t = OxmlElement("w:t")
            new_t.text = line
            new_t.set(XML_SPACE, "preserve")
            run.insert(pos + 2, new_t)
            pos += 2


DIALECT = core.Dialect(p_tag=W_P, tr_tag=W_TR, segments=_segments, on_modified=_preserve, finalize=_split_newlines)


# ---------------------------------------------------------------- 편지 병합 필드
def _mergefield_name(instr: str) -> str | None:
    m = _MERGEFIELD.search(instr or "")
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def _set_run_text(run: Any, text: str) -> None:
    ts = list(run.iter(W_T))
    if not ts:
        t = OxmlElement("w:t")
        run.append(t)
        ts = [t]
    ts[0].text = text
    ts[0].set(XML_SPACE, "preserve")
    for t in ts[1:]:
        t.text = ""


def _fldchar_type(run: Any) -> str | None:
    fc = run.find(W_FLDCHAR)
    return fc.get(qn("w:fldCharType")) if fc is not None else None


def unwrap_merge_fields(root: Any) -> int:
    """MERGEFIELD 필드를 «필드명» 텍스트로 바꾼다(필드 코드는 제거). 바꾼 개수 반환."""
    count = 0
    # 단순 필드: <w:fldSimple w:instr=" MERGEFIELD 연구제목 ">
    for fld in list(root.iter(W_FLDSIMPLE)):
        name = _mergefield_name(fld.get(qn("w:instr")) or "")
        if not name:
            continue
        parent = fld.getparent()
        idx = parent.index(fld)
        runs = [r for r in fld if r.tag == W_R]
        if not runs:
            runs = [OxmlElement("w:r")]
        _set_run_text(runs[0], f"«{name}»")
        for r in runs[1:]:
            _set_run_text(r, "")
        parent.remove(fld)
        for k, r in enumerate(runs):
            parent.insert(idx + k, r)
        count += 1
    # 복합 필드: begin → instrText → separate → 결과 run → end
    for p in list(root.iter(W_P)):
        runs = [r for r in p if r.tag == W_R]
        i = 0
        while i < len(runs):
            if _fldchar_type(runs[i]) != "begin":
                i += 1
                continue
            j, sep, end, nested, instr = i + 1, None, None, False, []
            while j < len(runs):
                kind = _fldchar_type(runs[j])
                if kind == "begin":
                    nested = True
                    break
                if kind == "separate":
                    sep = j
                elif kind == "end":
                    end = j
                    break
                elif sep is None:
                    instr.append("".join(t.text or "" for t in runs[j].iter(W_INSTR)))
                j += 1
            if nested or end is None:
                i += 1
                continue
            name = _mergefield_name("".join(instr))  # 필드 코드도 여러 run에 나뉠 수 있다
            if name:
                results = runs[sep + 1:end] if sep is not None else []
                control = [runs[i], *runs[i + 1:(sep if sep is not None else end)], runs[end]]
                if sep is not None:
                    control.append(runs[sep])
                if results:
                    _set_run_text(results[0], f"«{name}»")
                    for r in results[1:]:
                        _set_run_text(r, "")
                else:
                    new_run = OxmlElement("w:r")
                    _set_run_text(new_run, f"«{name}»")
                    p.insert(p.index(runs[i]), new_run)
                for r in control:
                    if r.getparent() is p:
                        p.remove(r)
                count += 1
            i = end + 1
    return count


# ---------------------------------------------------------------- 입출력
def _roots(doc: Any) -> list[Any]:
    roots = [doc.element]
    seen = {id(doc.part)}
    for rel in doc.part.rels.values():
        if rel.is_external or rel.reltype not in (RT.HEADER, RT.FOOTER, RT.FOOTNOTES, RT.ENDNOTES):
            continue
        part = rel.target_part
        element = getattr(part, "element", None)
        if element is not None and id(part) not in seen:
            seen.add(id(part))
            roots.append(element)
    return roots


def load(raw: bytes) -> Any:
    return load_document(BytesIO(raw))


def detect(raw: bytes) -> list[PH.Placeholder]:
    doc = load(raw)
    found: list[PH.Placeholder] = []
    for root in _roots(doc):
        unwrap_merge_fields(root)
        found.extend(core.detect(root, DIALECT))
    return found


def fill(raw: bytes, render: core.Render, list_items, stats: core.FillStats) -> bytes:
    doc = load(raw)
    for root in _roots(doc):
        unwrap_merge_fields(root)
        core.expand_repeats(root, DIALECT, render, list_items, stats)
        core.replace_all(root, DIALECT, render, stats)
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def plain_text(raw: bytes) -> str:
    """미리보기·검색용 본문 텍스트."""
    doc = load(raw)
    lines = []
    for root in _roots(doc):
        for p in root.iter(W_P):
            text = core.paragraph_text(p, DIALECT).strip()
            if text:
                lines.append(text)
    return "\n".join(lines)
