"""문서 변환: 마크다운, PDF(LibreOffice 또는 텍스트 → PDF), 타 기관 서식 → 우리 과제 입력값."""
from __future__ import annotations

import io
import re
from datetime import date
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from ..engine.ruleset import get_ruleset
from .extract_fields import NUMBERING, SectionParser
from .office import soffice_convert
from .text import IMAGE_TYPES, ExtractedText, detect_format, extract_text

OFFICE_FORMATS = {"docx", "xlsx", "pptx", "odf", "ole", "rtf", "html", "text"}


# ====================================================================== 마크다운
def to_markdown(extracted: ExtractedText, title: str = "") -> str:
    parser = SectionParser()
    out: list[str] = [f"# {title}", ""] if title else []
    table: list[list[str]] = []

    def flush_table() -> None:
        if not table:
            return
        width = max(len(r) for r in table)
        rows = [r + [""] * (width - len(r)) for r in table]
        out.append("| " + " | ".join(c.replace("|", "\\|") for c in rows[0]) + " |")
        out.append("|" + "---|" * width)
        for r in rows[1:]:
            out.append("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
        out.append("")
        table.clear()

    for raw in extracted.text.splitlines():
        line = raw.strip()
        if not line:
            flush_table()
            continue
        if " | " in line:
            table.append([c.strip() for c in line.split("|")])
            continue
        flush_table()
        parsed = parser.parse_line(line)
        if line.startswith("#") or parsed.kind == "heading":
            text = line.lstrip("#").strip()
            out += [f"## {text}", ""]
        else:
            out += [line, ""]
    flush_table()
    if extracted.warnings:
        out += ["---", *[f"> ⚠ {w}" for w in extracted.warnings]]
    return "\n".join(out).strip() + "\n"


# ====================================================================== PDF
def _font() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    name = "HYSMyeongJo-Medium"
    if name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(name))
    return name


def text_to_pdf(text: str, title: str = "") -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    font = _font()
    body = ParagraphStyle("body", fontName=font, fontSize=10, leading=15, wordWrap="CJK")
    head = ParagraphStyle("head", fontName=font, fontSize=14, leading=20, spaceAfter=8, wordWrap="CJK")
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=title or "변환 문서", leftMargin=50, rightMargin=50)
    story: list[Any] = []
    if title:
        story.append(Paragraph(escape(title), head))
    for line in text.splitlines():
        story.append(Paragraph(escape(line) or "&nbsp;", body) if line.strip() else Spacer(1, 6))
    doc.build(story or [Paragraph("(내용 없음)", body)])
    return buf.getvalue()


def image_to_pdf(raw: bytes) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    img = ImageReader(io.BytesIO(raw))
    w, h = img.getSize()
    pw, ph = A4
    scale = min((pw - 60) / w, (ph - 60) / h, 1.0)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.drawImage(img, 30, ph - 30 - h * scale, w * scale, h * scale)
    c.showPage()
    c.save()
    return buf.getvalue()


def to_pdf(filename: str, raw: bytes) -> bytes | None:
    """PDF로 변환. PDF는 그대로, 오피스 문서는 LibreOffice, HWP/HWPX 등은 추출 텍스트로 PDF 생성."""
    fmt = detect_format(filename, raw)
    if fmt == "pdf":
        return raw
    if fmt in IMAGE_TYPES:
        try:
            return image_to_pdf(raw)
        except Exception:
            return None
    if fmt in OFFICE_FORMATS:
        pdf = soffice_convert(filename, raw, "pdf")
        if pdf:
            return pdf
    extracted = extract_text(filename, raw)
    if extracted.text.strip():
        return text_to_pdf(extracted.text, Path(filename).name)
    return None


# ====================================================================== 타 기관 서식 → 과제 입력값
TEXT_KEYS = ("title", "objectives", "background", "methods", "target_population", "inclusion_criteria",
             "exclusion_criteria", "sample_size_justification", "data_items", "access_controls",
             "destruction_method", "waiver_impracticable_detail", "funding_source", "department")
INT_KEYS = ("expected_subjects", "retention_years")
DATE_KEYS = ("study_start", "study_end", "data_period_start", "data_period_end")
BOOL_KEYS = ("existing_data_only", "direct_contact", "external_transfer", "international_transfer", "data_linkage",
             "ai_ml_development", "multicenter", "rare_disease")
LIST_KEYS = {"data_sources": "data_sources", "identifiers_collected": "identifiers",
             "sensitive_categories": "sensitive_categories", "vulnerable_populations": "vulnerable_populations",
             "unstructured_data": "unstructured_data", "waiver_impracticable_reasons": "waiver_reasons"}
ENUM_KEYS = {"study_type": "study_types", "deid_level": "deid_levels"}


def external_to_protocol(fields: dict[str, dict[str, Any]], selected: set[str] | None = None) -> dict[str, Any]:
    """추출 결과 → Protocol 입력 dict (카탈로그 검증, 타입 변환). 'custom:<key>'는 custom_fields 로 모은다."""
    rs = get_ruleset()
    data: dict[str, Any] = {}
    custom: dict[str, Any] = {}
    for key, entry in fields.items():
        if selected is not None and key not in selected:
            continue
        value = entry.get("value") if isinstance(entry, dict) else entry
        if value in (None, "", []):
            if key == "funding_source" and value == "":
                data[key] = ""
            continue
        if key.startswith("custom:"):
            custom[key[7:]] = value
        elif key in TEXT_KEYS:
            data[key] = str(value).strip()[:300 if key == "title" else 8000]
        elif key in INT_KEYS:
            try:
                data[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        elif key in DATE_KEYS:
            try:
                data[key] = value if isinstance(value, date) else date.fromisoformat(str(value))
            except ValueError:
                continue
        elif key in BOOL_KEYS:
            data[key] = bool(value)
        elif key in LIST_KEYS:
            allowed = rs.catalog(LIST_KEYS[key])
            data[key] = [v for v in value if v in allowed]
        elif key in ENUM_KEYS:
            if value in rs.catalog(ENUM_KEYS[key]):
                data[key] = value
        elif key == "consent_plan" and value in ("obtain", "waiver"):
            data[key] = value
    if custom:
        data["custom_fields"] = custom
    return data


def strip_numbering(text: str) -> str:
    return NUMBERING.sub("", text).strip()


def safe_name(filename: str) -> str:
    return re.sub(r"[^\w.\-가-힣]", "_", Path(filename).name)[:120] or "document"
