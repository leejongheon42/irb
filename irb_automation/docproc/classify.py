"""서류 종류 자동 분류 (규칙 기반 + 선택적 AI 보조).

카탈로그 document_types 기준으로 제목 영역·본문·파일명의 키워드 점수를 매긴다.
기관마다 서식 명칭이 다르므로 규칙 세트 document_pipeline.classify_keywords 로 키워드를 추가할 수 있다.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..engine.ruleset import get_ruleset

# 서류 종류 → (키워드, 가중치)
DEFAULT_KEYWORDS: dict[str, list[tuple[str, float]]] = {
    "protocol": [("연구계획서", 6), ("연구 계획서", 6), ("protocol", 4), ("임상연구 계획", 5), ("연구 방법", 1.5),
                 ("연구 목적", 1.5), ("선정 기준", 1.5), ("제외 기준", 1.5), ("통계 분석", 1), ("연구 배경", 1)],
    "application": [("심의의뢰서", 8), ("심의 의뢰서", 8), ("심의신청서", 7), ("심의 신청서", 7), ("심의 의뢰", 4)],
    "cv": [("이력서", 8), ("curriculum vitae", 8), ("학력", 2), ("경력", 2), ("주요 논문", 2), ("연구 실적", 2)],
    "training_cert": [("이수증", 8), ("수료증", 7), ("교육 이수", 4), ("certificate of completion", 7), ("citi", 3),
                      ("이수 시간", 3), ("교육과정", 2)],
    "coi": [("이해상충", 7), ("conflict of interest", 7), ("재정적 이해관계", 4), ("이해관계", 2)],
    "consent_form": [("설명문", 5), ("동의서", 4), ("연구대상자 동의", 5), ("참여에 동의", 4), ("서명", 1.5),
                     ("자발적", 1.5), ("철회", 1.5)],
    "waiver_request": [("동의 면제 사유", 8), ("동의면제 사유", 8), ("서면동의 면제", 5), ("동의 면제", 3), ("동의면제", 3)],
    "exemption_request": [("심의면제", 6), ("심의 면제", 6), ("면제 확인", 4)],
    "crf": [("증례기록서", 8), ("case report form", 8), ("crf", 4), ("자료수집 항목", 4), ("자료 수집 항목표", 6)],
    "deid_plan": [("가명처리 계획", 8), ("가명처리계획", 8), ("비식별", 3), ("가명처리", 2.5), ("가명정보", 2), ("추가정보", 1.5)],
    "security_pledge": [("보안서약", 8), ("보안 서약", 8), ("서약서", 4), ("비밀유지", 3), ("준수할 것을 서약", 5)],
    "transfer_agreement": [("자료 제공 계약", 7), ("자료제공 계약", 7), ("data use agreement", 7), ("자료 이용 계약", 6),
                           ("계약서", 3), ("반출", 1.5)],
    "linkage_plan": [("결합 계획", 7), ("결합전문기관", 5), ("데이터 결합", 4)],
    "budget": [("연구비 예산", 7), ("예산서", 7), ("예산", 2), ("인건비", 2), ("직접비", 2)],
    "progress_report": [("경과보고", 7), ("경과 보고", 7), ("지속심의", 5), ("중간보고", 5)],
    "final_report": [("종료보고", 7), ("종료 보고", 7), ("결과보고서", 5), ("연구 종료", 2)],
    "external_irb_approval": [("심의결과 통지서", 6), ("승인서", 4), ("approval letter", 6), ("승인 통지", 4)],
    "site_list": [("참여기관 목록", 7), ("참여기관", 2.5), ("기관별 연구책임자", 4)],
    "amendment_table": [("변경 대비표", 8), ("변경대비표", 8), ("변경 전", 2), ("변경 후", 2)],
    "destruction_certificate": [("파기 확인서", 8), ("폐기 확인서", 8), ("파기 확인", 4), ("파기 일자", 3)],
}
# 연구계획서 구조 판정용 제목들
PROTOCOL_SECTIONS = ("연구 목적", "연구 배경", "연구 방법", "연구 대상", "선정 기준", "제외 기준", "대상자 수", "통계",
                     "자료 수집", "개인정보", "동의")


def keywords() -> dict[str, list[tuple[str, float]]]:
    merged = {k: list(v) for k, v in DEFAULT_KEYWORDS.items()}
    extra: dict[str, Any] = get_ruleset().policy("document_pipeline").get("classify_keywords", {}) or {}
    for doc_type, words in extra.items():
        for w in words or []:
            if isinstance(w, (list, tuple)) and len(w) == 2:
                merged.setdefault(doc_type, []).append((str(w[0]), float(w[1])))
            else:
                merged.setdefault(doc_type, []).append((str(w), 5.0))
    return merged


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())


def score_document(filename: str, text: str) -> list[tuple[str, float, list[str]]]:
    body = _squash(text[:20000])
    head = _squash(text[:400])
    name = _squash(Path(filename).stem)
    scores: list[tuple[str, float, list[str]]] = []
    for doc_type, words in keywords().items():
        score = 0.0
        hits: list[str] = []
        for word, weight in words:
            w = _squash(word)
            if not w:
                continue
            n = body.count(w)
            if n:
                score += weight * min(n, 3) ** 0.5
                if w in head:
                    score += weight * 2  # 제목 영역 가산
                hits.append(word)
            if w in name:
                score += weight * 1.5
                hits.append(f"파일명:{word}")
        if doc_type == "protocol":
            structure = sum(1 for s in PROTOCOL_SECTIONS if _squash(s) in body)
            if structure >= 4:
                score += structure * 1.5
                hits.append(f"계획서 구성 {structure}개 항목")
        if score:
            scores.append((doc_type, round(score, 2), hits))
    scores.sort(key=lambda x: -x[1])
    return scores


def classify_document(filename: str, text: str) -> tuple[str, float, str]:
    """(서류 종류, 신뢰도 0~1, 근거)."""
    if not text.strip():
        return "other", 0.0, "텍스트가 없어 분류할 수 없음"
    scores = score_document(filename, text)
    if not scores:
        return "other", 0.1, "일치하는 서식 키워드 없음"
    top_type, top, hits = scores[0]
    second = scores[1][1] if len(scores) > 1 else 0.0
    margin = (top - second) / top if top else 0.0
    strength = min(1.0, top / 12)
    confidence = round(min(0.97, 0.25 + 0.45 * strength + 0.3 * margin), 2)
    reason = "키워드: " + ", ".join(dict.fromkeys(hits[:6]))
    if len(scores) > 1:
        reason += f" (차순위 {get_ruleset().label('document_types', scores[1][0])} {scores[1][1]}점)"
    return top_type, confidence, reason


AI_CLASSIFY_SYSTEM = (
    "당신은 병원 IRB 사무국의 서류 분류 보조자입니다. 주어진 문서 앞부분을 읽고 가장 알맞은 서류 종류 하나를 고르십시오. "
    "병원마다 서식 이름이 다를 수 있으니 내용으로 판단하고, 확신이 없으면 other를 고르십시오."
)


def classify_with_ai(filename: str, text: str) -> tuple[str, float, str] | None:
    """AI 보조 분류(텍스트는 호출 전에 개인정보 마스킹되어 있어야 한다). 실패하면 None."""
    from ..ai import client

    catalog = get_ruleset().catalog("document_types")
    schema = {
        "type": "object",
        "properties": {
            "doc_type": {"type": "string", "enum": list(catalog)},
            "confidence": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["doc_type", "confidence", "reason"],
        "additionalProperties": False,
    }
    options = "\n".join(f"- {k}: {v}" for k, v in catalog.items())
    try:
        data = client.structured_call(
            system=AI_CLASSIFY_SYSTEM,
            content=f"<options>\n{options}\n</options>\n<filename>{Path(filename).name}</filename>\n<document>\n{text[:6000]}\n</document>",
            schema=schema, effort="low", max_tokens=2000, purpose="서류 분류",
        )
    except client.AIError:
        return None
    conf = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
    return data["doc_type"], round(conf, 2), "AI: " + str(data.get("reason", ""))[:200]
