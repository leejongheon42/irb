"""HWPX(한글 2010 이후 개방형 문서) 서식 채우기.

HWPX는 ZIP 안의 XML(OWPML)로 구성된다. 본문은 ``Contents/section*.xml`` 에 있고 텍스트는
``<hp:p><hp:run><hp:t>…</hp:t></hp:run></hp:p>`` 구조다. 머리글·바닥글·표 셀도 같은 문단 구조를 쓰므로
섹션 XML의 모든 ``hp:p`` 를 같은 규칙으로 처리한다.

- 원본 ZIP의 항목 순서·압축 방식을 보존하고, ``mimetype`` 은 첫 항목·무압축으로 기록한다.
- HWP(바이너리, 한글 2007 이전 형식)는 구조가 달라 지원하지 않는다.
"""
from __future__ import annotations

import re
import zipfile
from io import BytesIO
from typing import Any

from lxml import etree

from . import core
from . import placeholders as PH

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HP_P, HP_T, HP_TR, HP_LINEBREAK = f"{{{HP}}}p", f"{{{HP}}}t", f"{{{HP}}}tr", f"{{{HP}}}lineBreak"
MIMETYPE = b"application/hwp+zip"
SECTION_RE = re.compile(r"^Contents/section(\d+)\.xml$")

# 외부 엔티티·네트워크 접근 차단(XXE 방지)
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


def _segments(p: Any) -> list[core.Segment]:
    segs: list[core.Segment] = []
    for t in p.iter(HP_T):
        if core.owner(t, HP_P) is not p:
            continue
        segs.append(core.Segment(t, "text"))
        for child in t:  # <hp:tab/>, <hp:lineBreak/> 등 뒤의 텍스트
            segs.append(core.Segment(child, "tail"))
    return segs


def _normalize_newlines(root: Any) -> None:
    """값의 줄바꿈을 <hp:lineBreak/> 로 바꾼다."""
    for t in list(root.iter(HP_T)):
        pieces = [t.text or ""] + [child.tail or "" for child in t]
        if not any("\n" in piece for piece in pieces):
            continue
        tokens: list[Any] = []
        if t.text:
            tokens.append(t.text)
        for child in list(t):
            tail = child.tail
            child.tail = None
            tokens.append(child)
            if tail:
                tokens.append(tail)
            t.remove(child)
        t.text = None
        last: Any = None
        for token in tokens:
            if not isinstance(token, str):
                t.append(token)
                last = token
                continue
            for k, line in enumerate(token.split("\n")):
                if k:
                    last = etree.SubElement(t, HP_LINEBREAK)
                if not line:
                    continue
                if last is None:
                    t.text = (t.text or "") + line
                else:
                    last.tail = (last.tail or "") + line


DIALECT = core.Dialect(p_tag=HP_P, tr_tag=HP_TR, segments=_segments, finalize=_normalize_newlines)


def section_names(zf: zipfile.ZipFile) -> list[str]:
    names = [n for n in zf.namelist() if SECTION_RE.match(n)]
    return sorted(names, key=lambda n: int(SECTION_RE.match(n).group(1)))


def _parse(raw: bytes) -> Any:
    return etree.parse(BytesIO(raw), _PARSER)


def _serialize(tree: Any) -> bytes:
    standalone = tree.docinfo.standalone
    kwargs: dict[str, Any] = {"xml_declaration": True, "encoding": "UTF-8"}
    if standalone is not None:
        kwargs["standalone"] = standalone
    return etree.tostring(tree, **kwargs)


def detect(raw: bytes) -> list[PH.Placeholder]:
    found: list[PH.Placeholder] = []
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        for name in section_names(zf):
            found.extend(core.detect(_parse(zf.read(name)).getroot(), DIALECT))
    return found


def fill(raw: bytes, render: core.Render, list_items, stats: core.FillStats) -> bytes:
    replaced: dict[str, bytes] = {}
    with zipfile.ZipFile(BytesIO(raw)) as zin:
        for name in section_names(zin):
            tree = _parse(zin.read(name))
            root = tree.getroot()
            core.expand_repeats(root, DIALECT, render, list_items, stats)
            core.replace_all(root, DIALECT, render, stats)
            replaced[name] = _serialize(tree)
        infos = zin.infolist()
        ordered = [i for i in infos if i.filename == "mimetype"] + [i for i in infos if i.filename != "mimetype"]
        out = BytesIO()
        with zipfile.ZipFile(out, "w") as zout:
            for info in ordered:
                data = replaced.get(info.filename, None)
                if data is None:
                    data = zin.read(info.filename)
                new = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                new.compress_type = zipfile.ZIP_STORED if info.filename == "mimetype" else info.compress_type
                new.external_attr = info.external_attr
                new.create_system = info.create_system
                zout.writestr(new, data)
    return out.getvalue()


def plain_text(raw: bytes) -> str:
    lines = []
    with zipfile.ZipFile(BytesIO(raw)) as zf:
        for name in section_names(zf):
            for p in _parse(zf.read(name)).getroot().iter(HP_P):
                text = core.paragraph_text(p, DIALECT).strip()
                if text:
                    lines.append(text)
    return "\n".join(lines)
