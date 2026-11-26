"""업로드 문서의 서류 종류 추정.

1) 파일명 키워드(병원마다 서식 이름이 달라도 흔히 쓰는 표현 위주)
2) 파일명으로 확신이 낮으면 문서 텍스트 기반 분류기(docproc.classify, 선택 모듈)를 사용
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from unicodedata import normalize

from ..qc.pattern_store import ClassificationPatterns, default_classification

log = logging.getLogger(__name__)
_SPECIMEN_FORM_HEADER = re.compile(r"별지\s*제?\s*34\s*호\s*서식")
_RECORDS_PLAN_TITLE = re.compile(r"(?:연구)?자료(?:접근(?:권한)?(?:및)?(?:보관(?:및)?)?(?:폐기|파기)|관리)계획서")
_ETHICS_PLEDGE_TITLE = re.compile(r"생명윤리(?:(?:및)?자료이용)?준수서약(?:서)?")
_RECORDS_PERMISSION_TITLE = re.compile(r"(?:의무기록(?:내부)?(?:이용|열람|접근)|(?:기관|관리부서)자료이용)(?:허가|승인|확인)(?:서|증빙)")
_RECORDING_CHOICE_TITLE = re.compile(r"(?:기존(?:연구)?참여자용)?(?:음성)?녹음(?:추가설명(?:및)?)?(?:추가)?선택서")


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
    raw = normalize("NFC", Path(filename).stem)
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
    # The statutory form can also have a filename ending simply in "동의서".
    if (g.doc_type in ("other", "consent_form") and (allowed is None or "specimen_consent" in allowed)
            and _SPECIMEN_FORM_HEADER.search((extracted_text or "")[:500])):
        return Guess("specimen_consent", 0.97, "본문의 별지 제34호서식 표시", "classifier")
    stem = re.sub(r"^\d+", "", _normalize(normalize("NFC", Path(filename).stem)))
    heading = next((line.lstrip("# ").strip() for line in (extracted_text or "")[:500].splitlines()
                    if line.startswith("#")), "")
    for generic, role, pattern in (("protocol", "deid_plan", _RECORDS_PLAN_TITLE),
                                   ("security_pledge", "pledge", _ETHICS_PLEDGE_TITLE),
                                   ("consent_form", "recording_choice", _RECORDING_CHOICE_TITLE),
                                   ("transfer_agreement", "records_access_evidence", _RECORDS_PERMISSION_TITLE)):
        if (g.doc_type in (generic, "other") and (allowed is None or role in allowed)
                and (pattern.fullmatch(stem) or pattern.fullmatch(_normalize(heading)))):
            return Guess(role, 0.95, "제출 서류의 명시적 제목", "classifier")
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
