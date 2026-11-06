"""PDF 생성 보조: 한글 글꼴 선택.

한글이 모든 뷰어에서 보이려면 글꼴을 PDF에 내장해야 한다(CID 글꼴은 뷰어에 한글 언어팩이 없으면 공백으로 표시).
- IRB_PDF_FONT 환경변수(TTF/TTC 경로) → 시스템 한글 TTF 탐색 → 실패 시 CID 글꼴(HYGothic-Medium)
- embedded=False 이면 한글이 보이지 않을 수 있으므로 호출 측에서 ASCII 표기로 대체한다.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

CANDIDATES = (
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf",
    "/usr/share/fonts/truetype/unfonts-core/UnDotum.ttf",
    "/usr/share/fonts/truetype/baekmuk/gulim.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/Library/Fonts/AppleGothic.ttf",
    "C:/Windows/Fonts/malgun.ttf",
)


@lru_cache(maxsize=1)
def korean_font() -> tuple[str, bool]:
    """(등록된 글꼴 이름, 내장 여부)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    paths = [os.environ["IRB_PDF_FONT"]] if os.environ.get("IRB_PDF_FONT") else []
    for path in [*paths, *CANDIDATES]:
        if not Path(path).is_file():
            continue
        try:
            kwargs = {"subfontIndex": 0} if path.lower().endswith(".ttc") else {}
            pdfmetrics.registerFont(TTFont("IRBKorean", path, **kwargs))
            return "IRBKorean", True
        except Exception:  # 지원하지 않는 글꼴(CFF 기반 OTF 등)은 건너뛴다
            log.warning("PDF font not usable: %s", path)
    try:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
        return "HYGothic-Medium", False
    except Exception:
        return "Helvetica", False


def ascii_or(text: str, fallback: str, embedded: bool) -> str:
    """글꼴이 내장되지 않으면 한글이 포함된 문구를 ASCII 대체 문구로 바꾼다."""
    if embedded or text.isascii():
        return text
    return fallback
