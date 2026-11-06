"""브라우저 미리보기와 텍스트 추출.

- PDF·이미지·자동 작성 HTML은 그대로 표시
- HWP/HWPX/DOCX 등은 문서 변환 모듈(docproc.convert, 선택)로 PDF 변환 후 표시(결과 캐시)
- 변환이 불가하면 텍스트 추출(docproc.text, 선택) 결과를, 그것도 없으면 다운로드 안내
"""
from __future__ import annotations

import html
import io
import logging
import re
from pathlib import Path

from ..models import Document

log = logging.getLogger(__name__)

OFFICE_EXT = {".docx", ".hwpx", ".hwp", ".doc", ".xls", ".xlsx"}


def preview_kind(doc: Document) -> str:
    ext = Path(doc.filename).suffix.lower()
    mt = (doc.mime_type or "").lower()
    if ext == ".pdf" or mt == "application/pdf":
        return "pdf"
    if ext in (".png", ".jpg", ".jpeg") or mt.startswith("image/"):
        return "image"
    if ext in (".html", ".htm") or mt == "text/html":
        return "html"
    if ext in (".txt", ".csv"):
        return "text"
    if ext in OFFICE_EXT:
        return "office"
    return "none"


def read_bytes(doc: Document) -> bytes | None:
    if not doc.has_file:
        return None
    return Path(doc.stored_path).read_bytes()


def converted_pdf(doc: Document) -> bytes | None:
    """오피스·한글 문서를 PDF로 변환(변환 모듈이 있을 때). 결과는 원본 옆에 캐시한다."""
    if not doc.has_file:
        return None
    cache = Path(doc.stored_path + ".preview.pdf")
    if cache.is_file():
        return cache.read_bytes()
    try:
        from ..docproc.convert import to_pdf
    except ImportError:
        return None
    try:
        pdf = to_pdf(doc.filename, Path(doc.stored_path).read_bytes())
    except Exception:  # 변환 실패 시 텍스트 미리보기로 대체
        log.exception("preview conversion failed for document %s", doc.id)
        return None
    if pdf:
        cache.write_bytes(pdf)
    return pdf or None


def pdf_bytes_for(doc: Document) -> bytes | None:
    """PDF 원본 또는 변환본."""
    kind = preview_kind(doc)
    if kind == "pdf":
        return read_bytes(doc)
    if kind == "office":
        return converted_pdf(doc)
    return None


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _html_text(raw: bytes) -> str:
    text = _decode(raw)
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _pdf_text(raw: bytes) -> str | None:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            return None
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        log.exception("pdf text extraction failed")
        return None


def document_text(doc: Document, limit: int = 300_000) -> str | None:
    """비교·미리보기용 텍스트. 문서 인식 모듈이 있으면 우선 사용한다."""
    raw = read_bytes(doc)
    if raw is None:
        return None
    try:
        from ..docproc.text import extract_text
    except ImportError:
        extract_text = None
    if extract_text is not None:
        try:
            extracted = extract_text(doc.filename, raw, allow_ai_ocr=False)
            if extracted and extracted.text and extracted.text.strip():
                return extracted.text[:limit]
        except Exception:
            log.exception("text extraction failed for document %s", doc.id)
    kind = preview_kind(doc)
    if kind == "text":
        return _decode(raw)[:limit]
    if kind == "html":
        return _html_text(raw)[:limit]
    if kind == "pdf":
        text = _pdf_text(raw)
        return text[:limit] if text and text.strip() else None
    return None
