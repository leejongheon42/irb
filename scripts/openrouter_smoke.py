"""Exercise the nine OpenRouter purposes using synthetic documents only."""
# /// script
# requires-python = ">=3.10"
# dependencies = ["irb-automation", "httpx>=0.27", "jsonschema>=4.23", "pypdf>=5", "pypdfium2>=4.30", "Pillow>=10", "python-dotenv>=1"]
# [tool.uv.sources]
# irb-automation = { path = ".." }
# ///
# ─── How to run ───
# From the repository root, set OPENROUTER_API_KEY in .env and run
# uv run scripts/openrouter_smoke.py [purpose ...]
# Or, in the project environment, python -m scripts.openrouter_smoke [purpose ...].
# ──────────────────
from __future__ import annotations

import argparse
import io
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Final

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from irb_automation.ai import client as ai
from irb_automation.config import get_settings, override_settings
from irb_automation.docproc.classify import classify_with_ai
from irb_automation.docproc.extract_fields import ai_extract_fields
from irb_automation.docproc.text import extract_text
from irb_automation.engine.ai_review import run_ai_review
from irb_automation.knowledge.service import _ai_answer
from irb_automation.models import Base, Protocol, RevisionItem
from irb_automation.package.drafting import ai_drafts
from irb_automation.package.revision import draft_responses
from irb_automation.pipeline.service import AI_SUMMARY_SCHEMA, AI_SUMMARY_SYSTEM
from irb_automation.qc.ai_phase import run_ai
from irb_automation.qc.checks import QCDoc

TITLE: Final = "합성 연구자료 분석"
DOCUMENT: Final = (
    "연구계획서\n연구 제목: 합성 연구자료 분석\n연구 목적: 가상 관측 자료의 월별 변화를 비교한다.\n"
    "연구 방법: 합성 표본 120건을 통계 요약한다. 연구 기간은 2026년 1월부터 3월까지다.\n"
    "대상자: 가상 표본 120건. 환자 접촉과 실제 개인정보 수집은 없다."
)


def _prereview() -> str:
    review = run_ai_review({"title": TITLE, "objectives": DOCUMENT}, {"findings": []})
    if not review or "error" in review:
        raise ai.AIError(str(review.get("error")) if review else "사전검토 결과 없음")
    return review["summary"]


def _qc() -> str:
    doc = QCDoc("d1", "synthetic.txt", "protocol", DOCUMENT)
    _, result = run_ai([doc], [])
    if result["status"] != "done":
        raise ai.AIError(result["message"])
    return result["message"]


def _protocol() -> Protocol:
    return Protocol(title=TITLE, objectives="가상 자료의 월별 변화를 비교한다.", methods="합성 표본을 통계 요약한다.",
                    expected_subjects=120, study_type="retrospective", data_items="월별 합성 값")


def _draft() -> str:
    drafts, status = ai_drafts(_protocol(), ["background"])
    if not drafts.get("background"):
        raise ai.AIError(status)
    return drafts["background"]


def _revision() -> str:
    item = RevisionItem(id=1, request_text="합성 표본의 선정 기준을 구체화해 주십시오.")
    result = draft_responses(_protocol(), [item])[1]
    if result["source"] != "ai":
        raise ai.AIError("보완 답변이 규칙 기반으로 대체되었습니다.")
    return result["response"]


def _ocr() -> str:
    paths = (
        Path("C:/Windows/Fonts/malgun.ttf"),
        Path("/usr/share/fonts/truetype/nanum/NanumGothic.ttf"),
        Path("/System/Library/Fonts/AppleSDGothicNeo.ttc"),
    )
    font_path = next((path for path in paths if path.is_file()), None)
    marker = "합성자료 120건" if font_path else "SYNTHETIC SAMPLE 120"
    font = ImageFont.truetype(str(font_path), 40) if font_path else ImageFont.load_default(size=40)
    with Image.new("RGB", (850, 440), "white") as image, io.BytesIO() as pdf:
        ImageDraw.Draw(image).text((55, 80), marker, font=font, fill="black")
        image.save(pdf, "PDF")
        result = extract_text("synthetic-scan.pdf", pdf.getvalue(), allow_ai_ocr=True)
    expected = "합성자료120건" if font_path else "syntheticsample120"
    if result.method != "ai_ocr" or expected not in "".join(result.text.casefold().split()):
        raise ai.AIError("OCR 표본 수를 확인하지 못했습니다. " + " ".join(result.warnings))
    return result.text


def _classify() -> str:
    result = classify_with_ai("synthetic.txt", DOCUMENT)
    if not result or result[0] != "protocol":
        raise ai.AIError("합성 문서를 연구계획서로 분류하지 못했습니다.")
    return result[0]


def _extract() -> str:
    result = ai_extract_fields(DOCUMENT)
    if result.get("expected_subjects", {}).get("value") != 120:
        raise ai.AIError("합성 문서의 표본 수 120건을 추출하지 못했습니다.")
    return ", ".join(result)


def _summary() -> str:
    return ai.structured_call(system=AI_SUMMARY_SYSTEM, content=DOCUMENT, schema=AI_SUMMARY_SCHEMA,
                              purpose="summary")["summary"]


def _knowledge() -> str:
    snippet = "합성 연구팀 안내문에 따르면 시험용 접수 창구는 모의 포털 A입니다."
    hits = [{"title": "합성 연구팀 안내문", "ref": "테스트", "heading": "모의 접수", "snippet": snippet,
             "source_id": 1, "chunk_id": 1, "url": "", "kind": "faq"}]
    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            result = _ai_answer(db, "시험용 접수 창구는 어디입니까?", hits)
    finally:
        engine.dispose()
    if not result or not result["citations"] or "포털 A" not in result["answer"]:
        raise ai.AIError("합성 근거에 맞는 인용 답변을 받지 못했습니다.")
    return result["answer"]


CHECKS: Final[dict[str, Callable[[], str]]] = {
    "prereview": _prereview, "qc": _qc, "draft": _draft, "revision": _revision, "ocr": _ocr,
    "classify": _classify, "extract": _extract, "summary": _summary, "knowledge": _knowledge,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("purposes", nargs="*", metavar="PURPOSE", help=", ".join(CHECKS))
    args = parser.parse_args()
    purposes = args.purposes or list(CHECKS)
    for purpose in purposes:
        if purpose not in CHECKS:
            parser.error(f"unknown purpose: {purpose}")
    load_dotenv(Path.cwd() / ".env", encoding="utf-8-sig")
    settings = replace(get_settings(), ai_review_enabled=True, ai_provider="openrouter")
    if not settings.openrouter_api_key:
        print("OPENROUTER_API_KEY를 설정하십시오.", file=sys.stderr)
        return 2
    override_settings(settings)

    failed = 0
    for purpose in purposes:
        try:
            sample = CHECKS[purpose]()
            if not sample.strip():
                raise ai.AIError("응답이 비어 있습니다.")
        except ai.AIUnavailable as e:
            print(f"FAIL {purpose} · {e}", flush=True)
            return 2
        except ai.AIError as e:
            print(f"FAIL {purpose} · {e}", flush=True)
            failed += 1
        else:
            model = ai.recent_usage()[0]["model"]
            print(f"PASS {purpose} · {model} · {' '.join(sample.split())[:160]}", flush=True)
    return int(failed > 0)


if __name__ == "__main__":
    sys.exit(main())
