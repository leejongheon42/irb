"""업로드 문서의 서류 종류 추정.

1) 파일명 키워드(병원마다 서식 이름이 달라도 흔히 쓰는 표현 위주)
2) 파일명으로 확신이 낮으면 문서 텍스트 기반 분류기(docproc.classify, 선택 모듈)를 사용
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from ..qc.pattern_store import ClassificationPatterns, default_classification

log = logging.getLogger(__name__)


@dataclass
class Guess:
    doc_type: str
    confidence: float
    reason: str
    source: str  # filename|classifier|none

    def to_dict(self) -> dict:
        return {"doc_type": self.doc_type, "confidence": round(self.confidence, 2), "reason": self.reason,
                "source": self.source}


def _normalize(text: str) -> str:
    return re.sub(r"[\s_\-.()\[\]{}·,]+", "", text.lower())


def guess_from_filename(filename: str, *, patterns: ClassificationPatterns | None = None) -> Guess:
    config = patterns if patterns is not None else default_classification()
    raw = Path(filename).stem
    stem = _normalize(raw)
    if not stem:
        return Guess("other", 0.0, "파일명 없음", "none")
    for doc_type, words in config.filename.items():
        for expression in words:
            w = expression.value
            if expression.is_regex:
                if expression.matches(raw):
                    return Guess(doc_type, 0.9, f"파일명 '{w}'", "filename")
                continue
            nw = _normalize(w)
            if not nw:
                continue
            # 짧은 영문 약어(cv, coi 등)는 단어 경계가 있을 때만 인정해 오탐을 줄인다.
            if nw.isascii() and len(nw) <= 3:
                if re.search(rf"(^|[^a-z]){re.escape(nw)}([^a-z]|$)", re.sub(r"[\s_\-.]+", " ", raw.lower())):
                    return Guess(doc_type, 0.75, f"파일명 '{w}'", "filename")
                continue
            if nw in stem:
                return Guess(doc_type, 0.9 if len(nw) >= 4 else 0.8, f"파일명 '{w}'", "filename")
    return Guess("other", 0.0, "파일명으로 판단 불가", "none")


def guess_doc_type(filename: str, raw: bytes | None = None, *, allowed: set[str] | None = None,
                   extracted_text: str | None = None, patterns: ClassificationPatterns | None = None) -> Guess:
    """파일명 → (필요 시) 텍스트 분류기 순서로 서류 종류를 추정한다."""
    g = guess_from_filename(filename, patterns=patterns)
    if allowed is not None and g.doc_type not in allowed:
        g = Guess("other", 0.0, g.reason, "none")
    if g.confidence >= 0.75 or (raw is None and extracted_text is None):
        return g
    try:  # 문서 인식 모듈(선택): 텍스트 추출 + 분류
        from ..docproc.classify import classify_document
        from ..docproc.text import extract_text
    except ImportError:
        return g
    try:
        if extracted_text is None and raw is not None:
            extracted_text = extract_text(filename, raw, allow_ai_ocr=False).text
        if patterns is None:
            doc_type, confidence, reason = classify_document(filename, extracted_text or "")
        else:
            doc_type, confidence, reason = classify_document(filename, extracted_text or "", patterns=patterns)
    except Exception:  # 분류 실패는 업로드를 막지 않는다
        log.exception("document classification failed for %s", filename)
        return g
    if doc_type and (allowed is None or doc_type in allowed) and float(confidence) > g.confidence:
        return Guess(doc_type, float(confidence), f"내용 분석: {reason}", "classifier")
    return g
