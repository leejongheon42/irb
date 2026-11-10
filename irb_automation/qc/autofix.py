"""자동 교정: 원본 서식(표·머리글·바닥글·글꼴)을 그대로 두고 지정한 문구만 고친 파일을 만든다.

- DOCX
  - ``tracked``: Word 변경 내용 추적(w:del / w:ins)으로 고친다. Word·한컴오피스에서 변경 내용을 보고 수락/거부할 수 있다.
  - ``clean``  : 바로 고친 제출용 본. 고친 부분은 노란 형광펜으로 표시(``highlight=False``면 표시 없음).
  - 글꼴 통일(``font``): 모든 글자의 한글·영문 글꼴을 지정한 글꼴로 맞춘다.
- HWPX: 같은 문단 구조(hp:p/hp:t)에서 문구를 바꾼다(변경 추적은 지원하지 않음).
- TXT: 문자열 치환.

교정 단위 fix = {"old": 원문, "new": 고친 문구, "all": 모든 위치 여부}
"""
from __future__ import annotations

import copy
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
BOUNDARY = "\x00"  # 탭·줄바꿈·그림 등 텍스트가 아닌 런: 교정 범위가 이를 넘지 않게 한다
AUTHOR = "IRB 서류 자동교정"


def w(tag: str) -> str:
    return f"{{{W}}}{tag}"


class AutofixError(Exception):
    pass


@dataclass
class FixReport:
    applied: dict[int, int] = field(default_factory=dict)  # fix index → 적용 횟수
    font: str | None = None

    @property
    def total(self) -> int:
        return sum(self.applied.values())


# ================================================================== DOCX
def _run_text(r: Any) -> str:
    parts = []
    for child in r:
        tag = etree.QName(child).localname if isinstance(child.tag, str) else ""
        if tag == "rPr":
            continue
        if tag == "t":
            parts.append(child.text or "")
        else:  # tab, br, drawing, fldChar, instrText ...
            parts.append(BOUNDARY)
    return "".join(parts)


def _is_plain(r: Any) -> bool:
    return all((etree.QName(c).localname in ("rPr", "t")) for c in r if isinstance(c.tag, str))


def _set_text(r: Any, text: str) -> None:
    ts = r.findall(w("t"))
    for t in ts[1:]:
        r.remove(t)
    t = ts[0] if ts else etree.SubElement(r, w("t"))
    t.text = text
    t.set(XML_SPACE, "preserve")


def _runs(p: Any) -> list[Any]:
    """문단 안의 편집 가능한 런(변경 추적으로 삭제된 런은 제외)."""
    out = []
    for r in p.iter(w("r")):
        anc = r.getparent()
        skip = False
        while anc is not None and anc is not p:
            if etree.QName(anc).localname in ("del", "moveFrom"):
                skip = True
                break
            anc = anc.getparent()
        if not skip and r.getparent() is not None:
            out.append(r)
    return out


def _split(r: Any, k: int) -> Any:
    """평문 런 r을 k번째 글자에서 둘로 나누고 뒤쪽 런을 반환."""
    text = _run_text(r)
    second = copy.deepcopy(r)
    _set_text(r, text[:k])
    _set_text(second, text[k:])
    r.addnext(second)
    return second


def _isolate(p: Any, start: int, end: int) -> list[Any] | None:
    """[start, end) 구간이 런 경계와 일치하도록 런을 나누고 해당 런 목록을 반환."""
    runs = _runs(p)
    pos = 0
    bounds = []
    for r in runs:
        t = _run_text(r)
        bounds.append((r, pos, pos + len(t)))
        pos += len(t)
    covered = [(r, a, b) for r, a, b in bounds if b > start and a < end]
    if not covered or any(not _is_plain(r) for r, _, _ in covered):
        return None
    first, a0, _ = covered[0]
    if start > a0:
        first = _split(first, start - a0)
        covered[0] = (first, start, covered[0][2])
    last, al, bl = covered[-1]
    if end < bl:
        _split(last, end - al)
    # 다시 계산(분할 반영)
    runs = _runs(p)
    pos = 0
    out = []
    for r in runs:
        t = _run_text(r)
        if pos >= start and pos + len(t) <= end and t:
            out.append(r)
        pos += len(t)
    return out or None


class _Ids:
    def __init__(self, root: Any):
        used = [int(v) for el in root.iter() for k, v in el.attrib.items()
                if k == w("id") and v.lstrip("-").isdigit()]
        self.n = max(used, default=0) + 1000

    def next(self) -> str:
        self.n += 1
        return str(self.n)


def _highlight(r: Any) -> None:
    rpr = r.find(w("rPr"))
    if rpr is None:
        rpr = etree.Element(w("rPr"))
        r.insert(0, rpr)
    for h in rpr.findall(w("highlight")):
        rpr.remove(h)
    h = etree.SubElement(rpr, w("highlight"))
    h.set(w("val"), "yellow")


def _replace_in_paragraph(p: Any, old: str, new: str, every: bool, mode: str, ids: _Ids, highlight: bool) -> int:
    count = 0
    start = 0
    date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    while True:
        combined = "".join(_run_text(r) for r in _runs(p))
        i = combined.find(old, start)
        if i < 0:
            break
        matched = _isolate(p, i, i + len(old))
        if not matched:
            start = i + 1
            continue
        if mode == "tracked":
            d = etree.Element(w("del"))
            d.set(w("id"), ids.next())
            d.set(w("author"), AUTHOR)
            d.set(w("date"), date)
            matched[0].addprevious(d)
            template = copy.deepcopy(matched[0])
            for r in matched:
                d.append(r)  # 원래 위치에서 del 안으로 이동
                for t in r.findall(w("t")):
                    t.tag = w("delText")
                    t.set(XML_SPACE, "preserve")
            ins = etree.Element(w("ins"))
            ins.set(w("id"), ids.next())
            ins.set(w("author"), AUTHOR)
            ins.set(w("date"), date)
            _set_text(template, new)
            ins.append(template)
            d.addnext(ins)
        else:
            _set_text(matched[0], new)
            if highlight:
                _highlight(matched[0])
            for r in matched[1:]:
                r.getparent().remove(r)
        count += 1
        start = i + len(new)
        if not every:
            break
    return count


def _unify_font(root: Any, font: str) -> None:
    for r in root.iter(w("r")):
        rpr = r.find(w("rPr"))
        if rpr is None:
            rpr = etree.Element(w("rPr"))
            r.insert(0, rpr)
        f = rpr.find(w("rFonts"))
        if f is None:
            f = etree.Element(w("rFonts"))
            rpr.insert(0, f)
        for attr in ("ascii", "hAnsi", "eastAsia", "cs"):
            f.set(w(attr), font)
        for attr in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            f.attrib.pop(w(attr), None)


def fix_docx(raw: bytes, fixes: list[dict], mode: str = "clean", font: str | None = None,
             highlight: bool = True) -> tuple[bytes, FixReport]:
    from ..doctemplates.docx_engine import _roots, load

    try:
        doc = load(raw)
    except Exception as e:
        raise AutofixError(f"DOCX를 열 수 없습니다: {e}") from e
    report = FixReport(font=font)
    roots = _roots(doc)
    ids = _Ids(roots[0])
    for idx, fx in enumerate(fixes):
        old, new = fx.get("old", ""), fx.get("new", "")
        if not old or old == new:
            continue
        n = 0
        for root in roots:
            for p in list(root.iter(w("p"))):
                n += _replace_in_paragraph(p, old, new, bool(fx.get("all", True)), mode, ids, highlight)
                if n and not fx.get("all", True):
                    break
            if n and not fx.get("all", True):
                break
        report.applied[idx] = n
    if font:
        for root in roots:
            _unify_font(root, font)
    if mode == "tracked":
        _ensure_track_view(doc)
    out = BytesIO()
    doc.save(out)
    return out.getvalue(), report


def _ensure_track_view(doc: Any) -> None:
    """문서를 열면 변경 내용이 보이도록 trackRevisions 설정을 켠다."""
    try:
        settings = doc.settings.element
    except Exception:  # pragma: no cover
        return
    if settings.find(w("trackRevisions")) is None:
        settings.insert(0, etree.Element(w("trackRevisions")))


# ================================================================== HWPX
def fix_hwpx(raw: bytes, fixes: list[dict]) -> tuple[bytes, FixReport]:
    from ..doctemplates.hwpx_engine import HP_P, HP_T, _parse, _serialize, section_names

    report = FixReport()
    replaced: dict[str, bytes] = {}
    try:
        zin = zipfile.ZipFile(BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise AutofixError("HWPX 파일을 열 수 없습니다.") from e
    with zin:
        trees = {name: _parse(zin.read(name)) for name in section_names(zin)}
        for idx, fx in enumerate(fixes):
            old, new = fx.get("old", ""), fx.get("new", "")
            if not old or old == new:
                continue
            n = 0
            for tree in trees.values():
                for p in tree.getroot().iter(HP_P):
                    n += _replace_segments([t for t in p.iter(HP_T)], old, new, bool(fx.get("all", True)))
            report.applied[idx] = n
        for name, tree in trees.items():
            replaced[name] = _serialize(tree)
        infos = zin.infolist()
        ordered = [i for i in infos if i.filename == "mimetype"] + [i for i in infos if i.filename != "mimetype"]
        out = BytesIO()
        with zipfile.ZipFile(out, "w") as zout:
            for info in ordered:
                data = replaced.get(info.filename) or zin.read(info.filename)
                ni = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                ni.compress_type = zipfile.ZIP_STORED if info.filename == "mimetype" else info.compress_type
                ni.external_attr = info.external_attr
                zout.writestr(ni, data)
    return out.getvalue(), report


def _replace_segments(ts: list[Any], old: str, new: str, every: bool) -> int:
    count = 0
    start = 0
    while True:
        texts = [t.text or "" for t in ts]
        combined = "".join(texts)
        i = combined.find(old, start)
        if i < 0:
            return count
        starts, pos = [], 0
        for t in texts:
            starts.append(pos)
            pos += len(t)
        end = i + len(old)
        si = max(k for k, s in enumerate(starts) if s <= i)
        ei = max(k for k, s in enumerate(starts) if s < end)
        if si == ei:
            texts[si] = texts[si][:i - starts[si]] + new + texts[si][end - starts[si]:]
        else:
            texts[si] = texts[si][:i - starts[si]] + new
            for k in range(si + 1, ei):
                texts[k] = ""
            texts[ei] = texts[ei][end - starts[ei]:]
        for t, txt in zip(ts, texts):
            t.text = txt
        count += 1
        start = i + len(new)
        if not every:
            return count


# ================================================================== 공통
def apply(filename: str, raw: bytes, fixes: list[dict], *, mode: str = "clean", font: str | None = None,
          highlight: bool = True) -> tuple[bytes, FixReport, str]:
    """(교정된 파일, 보고, 실제 적용 방식). 지원하지 않는 형식이면 AutofixError."""
    from ..docproc.text import detect_format

    fmt = detect_format(filename, raw)
    if fmt == "docx":
        data, rep = fix_docx(raw, fixes, mode=mode, font=font, highlight=highlight)
        return data, rep, mode
    if fmt == "hwpx":
        data, rep = fix_hwpx(raw, fixes)
        return data, rep, "clean"
    if fmt == "text":
        text = raw.decode("utf-8", errors="replace")
        rep = FixReport()
        for idx, fx in enumerate(fixes):
            old, new = fx.get("old", ""), fx.get("new", "")
            if old and old != new:
                rep.applied[idx] = text.count(old) if fx.get("all", True) else min(1, text.count(old))
                text = text.replace(old, new) if fx.get("all", True) else text.replace(old, new, 1)
        return text.encode("utf-8"), rep, "clean"
    raise AutofixError("자동 교정은 DOCX·HWPX·TXT 원본에서만 가능합니다. PDF·HWP는 원본(DOCX/HWPX)을 올려 주십시오.")


def can_fix(filename: str, raw: bytes | None = None) -> bool:
    from ..docproc.text import detect_format

    return detect_format(filename, raw or b"") in ("docx", "hwpx", "text") if raw else \
        filename.lower().endswith((".docx", ".hwpx", ".txt"))
