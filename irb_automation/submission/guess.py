"""업로드 문서의 서류 종류 추정.

1) 파일명 키워드(병원마다 서식 이름이 달라도 흔히 쓰는 표현 위주)
2) 파일명으로 확신이 낮으면 문서 텍스트 기반 분류기(docproc.classify, 선택 모듈)를 사용
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# 순서가 중요: 더 구체적인 표현을 먼저 검사한다(예: '동의 면제 사유서'는 '동의서'보다 먼저).
KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("waiver_request", ("동의면제", "면제사유", "서면동의면제", "waiver")),
    ("exemption_request", ("심의면제", "면제확인", "면제신청", "exemption", "exempt")),
    ("deid_plan", ("가명처리", "비식별", "익명처리", "deidentification", "deid", "pseudonym")),
    ("linkage_plan", ("결합계획", "데이터결합", "결합신청", "linkage")),
    ("amendment_table", ("변경대비", "대비표", "변경사항", "amendment")),
    ("security_pledge", ("보안서약", "비밀유지서약", "서약서", "pledge", "nda")),
    ("consent_form", ("동의서", "설명문", "설명서", "consent", "icf")),
    ("training_cert", ("교육이수", "이수증", "수료증", "이수확인", "교육", "certificate", "citi", "gcp")),
    ("cv", ("이력서", "약력", "경력", "curriculum", "resume", "cv")),
    ("coi", ("이해상충", "이해관계", "conflictofinterest", "coi")),
    ("crf", ("증례기록", "자료수집", "수집항목", "변수목록", "항목표", "crf")),
    ("progress_report", ("경과보고", "중간보고", "지속심의", "progress")),
    ("final_report", ("종료보고", "결과보고", "최종보고", "finalreport")),
    ("external_irb_approval", ("타기관", "승인통지", "승인서", "approvalletter")),
    ("site_list", ("참여기관", "기관목록", "sitelist")),
    ("destruction_certificate", ("파기확인", "파기", "destruction")),
    ("budget", ("예산", "연구비", "budget")),
    ("transfer_agreement", ("자료제공", "제공계약", "반출", "계약서", "협약서", "dua", "agreement")),
    ("application", ("심의의뢰", "심의신청", "의뢰서", "신청서", "application")),
    ("protocol", ("연구계획", "계획서", "프로토콜", "protocol")),
]


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


def guess_from_filename(filename: str) -> Guess:
    stem = _normalize(Path(filename).stem)
    if not stem:
        return Guess("other", 0.0, "파일명 없음", "none")
    for doc_type, words in KEYWORDS:
        for w in words:
            nw = _normalize(w)
            if not nw:
                continue
            # 짧은 영문 약어(cv, coi 등)는 단어 경계가 있을 때만 인정해 오탐을 줄인다.
            if nw.isascii() and len(nw) <= 3:
                if re.search(rf"(^|[^a-z]){re.escape(nw)}([^a-z]|$)", re.sub(r"[\s_\-.]+", " ", Path(filename).stem.lower())):
                    return Guess(doc_type, 0.75, f"파일명 '{w}'", "filename")
                continue
            if nw in stem:
                return Guess(doc_type, 0.9 if len(nw) >= 4 else 0.8, f"파일명 '{w}'", "filename")
    return Guess("other", 0.0, "파일명으로 판단 불가", "none")


def guess_doc_type(filename: str, raw: bytes | None = None, *, allowed: set[str] | None = None) -> Guess:
    """파일명 → (필요 시) 텍스트 분류기 순서로 서류 종류를 추정한다."""
    g = guess_from_filename(filename)
    if allowed is not None and g.doc_type not in allowed:
        g = Guess("other", 0.0, g.reason, "none")
    if g.confidence >= 0.75 or raw is None:
        return g
    try:  # 문서 인식 모듈(선택): 텍스트 추출 + 분류
        from ..docproc.classify import classify_document
        from ..docproc.text import extract_text
    except ImportError:
        return g
    try:
        extracted = extract_text(filename, raw, allow_ai_ocr=False)
        doc_type, confidence, reason = classify_document(filename, extracted.text)
    except Exception:  # 분류 실패는 업로드를 막지 않는다
        log.exception("document classification failed for %s", filename)
        return g
    if doc_type and (allowed is None or doc_type in allowed) and float(confidence) > g.confidence:
        return Guess(doc_type, float(confidence), f"내용 분석: {reason}", "classifier")
    return g
