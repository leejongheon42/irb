"""Find exact citations and render their highlighted regions on original PDF pages."""
from __future__ import annotations

import io
import re
from contextlib import closing
from typing import NotRequired, TypedDict

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from PIL import ImageDraw

from .pdf_images import _RENDER_LOCK

Box = tuple[float, float, float, float]


class SourcePosition(TypedDict):
    line: int
    last_line: int
    page: NotRequired[int]
    boxes: NotRequired[list[Box]]


class SourceMatch(TypedDict):
    position: SourcePosition | None
    ambiguous: bool


def _index(text: str) -> tuple[str, list[int], list[int]]:
    """Index visible characters by extracted line and UTF-16 text offset."""
    chars: list[str] = []
    lines: list[int] = []
    offsets: list[int] = []
    line = 0
    offset = 0
    for row in text.splitlines(keepends=True):
        if row.strip():
            line += 1
        for char in row:
            if not char.isspace():
                chars.append(char)
                lines.append(line)
                offsets.append(offset)
            offset += 1 + (ord(char) > 0xFFFF)
    return "".join(chars), lines, offsets


def _line_boxes(textpage: pdfium.PdfTextPage, lines: list[int], offsets: list[int]) -> list[Box]:
    boxes: list[Box] = []
    start = 0
    for end in range(1, len(lines) + 1):
        if end < len(lines) and lines[end] == lines[start]:
            continue
        first = pdfium_raw.FPDFText_GetCharIndexFromTextIndex(textpage, offsets[start])
        last = pdfium_raw.FPDFText_GetCharIndexFromTextIndex(textpage, offsets[end - 1])
        if 0 <= first <= last:
            for rect in range(textpage.count_rects(first, last - first + 1)):
                boxes.append(textpage.get_rect(rect))
        start = end
    return boxes


def _find_on_page(text: str, patterns: dict[str, re.Pattern[str]],
                  textpage: pdfium.PdfTextPage | None = None) -> dict[str, SourceMatch]:
    body, lines, offsets = _index(text)
    hits: dict[str, SourceMatch] = {}
    for quote, pattern in patterns.items():
        matches = pattern.finditer(body)
        first = next(matches, None)
        if first is None:
            continue
        if next(matches, None) is not None:
            hits[quote] = {"position": None, "ambiguous": True}
            continue
        start, end = first.span(1)
        position: SourcePosition = {"line": lines[start], "last_line": lines[end - 1]}
        if textpage is not None:
            position["boxes"] = _line_boxes(textpage, lines[start:end], offsets[start:end])
        hits[quote] = {"position": position, "ambiguous": False}
    return hits


def locate_source(quotes: set[str], *, pdf: bytes | None = None, text: str | None = None) -> dict[str, SourceMatch]:
    """Resolve exact citations, ignoring whitespace and rejecting ambiguous matches."""
    patterns = {quote: re.compile(f"(?=({re.escape(needle)}))")
                for quote in quotes if (needle := "".join(quote.split()))}
    if text is not None:
        return _find_on_page(text, patterns)
    if pdf is None or not patterns:
        return {}
    found: dict[str, SourceMatch] = {}
    with _RENDER_LOCK, pdfium.PdfDocument(pdf) as document:
        for index in range(len(document)):
            with closing(document[index]) as page, closing(page.get_textpage()) as textpage:
                matches = _find_on_page(textpage.get_text_range(errors="replace"), patterns, textpage)
            for quote, match in matches.items():
                if quote in found:
                    match = {"position": None, "ambiguous": True}
                if match["position"] is not None:
                    match["position"]["page"] = index + 1
                found[quote] = match
                if match["ambiguous"]:
                    patterns.pop(quote)
            if not patterns:
                break
    return found


def highlighted_page(pdf: bytes, position: SourcePosition) -> bytes:
    """Render one original page and outline the verified text in red."""
    with _RENDER_LOCK, pdfium.PdfDocument(pdf) as document:
        with closing(document[position["page"] - 1]) as page:
            scale = min(2, 2200 / max(page.get_size()))
            with closing(page.render(scale=scale)) as bitmap, bitmap.to_pil().convert("RGB") as image:
                coordinates = bitmap.get_posconv(page)
                draw = ImageDraw.Draw(image)
                for left, bottom, right, top in position["boxes"]:
                    x1, y1 = coordinates.to_bitmap(left, bottom)
                    x2, y2 = coordinates.to_bitmap(right, top)
                    draw.rectangle((min(x1, x2) - 3, min(y1, y2) - 3, max(x1, x2) + 3, max(y1, y2) + 3),
                                   outline="#d00020", width=3)
                with io.BytesIO() as output:
                    image.save(output, format="PNG")
                    return output.getvalue()
