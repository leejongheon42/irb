"""Find exact citations and render their highlighted regions on original PDF pages."""
from __future__ import annotations

import io
from contextlib import closing
from typing import TypedDict

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from PIL import ImageDraw

from .pdf_images import _RENDER_LOCK

Box = tuple[float, float, float, float]


class _LinePosition(TypedDict):
    line: int
    last_line: int


class SourcePosition(_LinePosition, total=False):
    page: int
    boxes: list[Box]


class SourceMatch(TypedDict):
    position: SourcePosition | None
    ambiguous: bool


def _index(text: str, *, paginated: bool) -> tuple[str, list[int], list[int]]:
    """Index visible characters by extracted line and UTF-16 text offset."""
    chars: list[str] = []
    lines: list[int] = []
    offsets: list[int] = []
    line = 0
    offset = 0
    for row in text.splitlines(keepends=True):
        if not paginated or row.strip():
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


def _find_on_page(text: str, needles: dict[str, str],
                  textpage: pdfium.PdfTextPage | None = None) -> dict[str, SourceMatch]:
    body, lines, offsets = _index(text, paginated=textpage is not None)
    hits: dict[str, SourceMatch] = {}
    for quote, needle in needles.items():
        start = body.find(needle)
        if start < 0:
            continue
        if body.find(needle, start + 1) >= 0:
            hits[quote] = {"position": None, "ambiguous": True}
            continue
        end = start + len(needle)
        position: SourcePosition = {"line": lines[start], "last_line": lines[end - 1]}
        if textpage is not None:
            position["boxes"] = _line_boxes(textpage, lines[start:end], offsets[start:end])
        hits[quote] = {"position": position, "ambiguous": False}
    return hits


def locate_source(quotes: set[str], *, pdf: bytes | None = None, text: str | None = None) -> dict[str, SourceMatch]:
    """Resolve exact citations, ignoring whitespace and rejecting ambiguous matches."""
    needles = {quote: needle for quote in quotes if (needle := "".join(quote.split()))}
    if text is not None:
        return _find_on_page(text, needles)
    if pdf is None or not needles:
        return {}
    found: dict[str, SourceMatch] = {}
    with _RENDER_LOCK, pdfium.PdfDocument(pdf) as document:
        for index in range(len(document)):
            with closing(document[index]) as page, closing(page.get_textpage()) as textpage:
                count = textpage.count_chars()
                if not count:
                    continue
                matches = _find_on_page(textpage.get_text_range(0, count, errors="replace"), needles, textpage)
            for quote, match in matches.items():
                if quote in found:
                    match = {"position": None, "ambiguous": True}
                if match["position"] is not None:
                    match["position"]["page"] = index + 1
                found[quote] = match
                if match["ambiguous"]:
                    needles.pop(quote)
            if not needles:
                break
    return found


def highlighted_page(pdf: bytes, position: SourcePosition, *, preview: bool = False) -> bytes:
    """Render a page or its relevant excerpt with the verified text outlined in red."""
    with _RENDER_LOCK, pdfium.PdfDocument(pdf) as document:
        with closing(document[position["page"] - 1]) as page:
            scale = min(2, 2200 / max(page.get_size()))
            with closing(page.render(scale=scale)) as bitmap, bitmap.to_pil().convert("RGB") as image:
                coordinates = bitmap.get_posconv(page)
                draw = ImageDraw.Draw(image)
                rectangles: list[tuple[int, int, int, int]] = []
                for left, bottom, right, top in position["boxes"]:
                    x1, y1 = coordinates.to_bitmap(left, bottom)
                    x2, y2 = coordinates.to_bitmap(right, top)
                    rectangle = (max(0, min(x1, x2) - 3), max(0, min(y1, y2) - 3),
                                 min(image.width - 1, max(x1, x2) + 3), min(image.height - 1, max(y1, y2) + 3))
                    if rectangle[0] >= rectangle[2] or rectangle[1] >= rectangle[3]:
                        continue
                    rectangles.append(rectangle)
                    draw.rectangle(rectangle, outline="#d00020", width=3)
                with io.BytesIO() as output:
                    if preview and rectangles:
                        left = max(0, min(box[0] for box in rectangles) - 48)
                        right = min(image.width, max(max(box[2] for box in rectangles) + 48, left + image.width // 2))
                        top = max(0, min(box[1] for box in rectangles) - 72)
                        bottom = min(image.height, max(box[3] for box in rectangles) + 72)
                        with image.crop((left, top, right, bottom)) as excerpt:
                            excerpt.save(output, format="PNG")
                    else:
                        image.save(output, format="PNG")
                    return output.getvalue()
