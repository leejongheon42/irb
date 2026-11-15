"""Render a bounded batch of PDF pages to PNG for vision OCR."""
from __future__ import annotations

import io
import threading
from contextlib import closing
from typing import Final

from ..ai.client import AIError

# PDFium cannot be called simultaneously from several pipeline threads.
_RENDER_LOCK: Final = threading.Lock()


def render_pdf_pages(raw: bytes, start: int, stop: int) -> list[bytes]:
    """Render the zero-based page range, with a cap on image dimensions."""
    try:
        import pypdfium2 as pdfium
    except ImportError as e:
        raise AIError("PDF OCR용 pypdfium2와 Pillow를 설치하십시오.") from e

    images: list[bytes] = []
    try:
        with _RENDER_LOCK, pdfium.PdfDocument(raw) as document:
            for index in range(start, stop):
                with closing(document[index]) as page:
                    scale = min(2, 2200 / max(page.get_size()))
                    with closing(page.render(scale=scale)) as bitmap, bitmap.to_pil() as image, io.BytesIO() as png:
                        image.save(png, format="PNG")
                        images.append(png.getvalue())
    except (pdfium.PdfiumError, OSError, ImportError) as e:
        raise AIError(f"PDF OCR용 이미지 변환 실패({type(e).__name__})") from e
    return images
