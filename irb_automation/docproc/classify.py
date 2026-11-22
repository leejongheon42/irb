"""서류 종류 자동 분류 (규칙 기반 + 선택적 AI 보조).

카탈로그 document_types 기준으로 제목 영역·본문·파일명의 키워드 점수를 매긴다.
서비스에서는 실행 시 DB에서 읽은 분류 규칙을 사용한다.
"""
from __future__ import annotations

import re
from itertools import chain
from pathlib import Path

from ..engine.ruleset import get_ruleset
from ..qc.pattern_store import ClassificationPatterns, default_classification
from ..qc.patterns import Expression


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())


def score_document(filename: str, text: str, *,
                   patterns: ClassificationPatterns | None = None) -> list[tuple[str, float, list[str]]]:
    config = patterns if patterns is not None else default_classification()
    raw_body, raw_head, raw_name = text[:20000], text[:400], Path(filename).stem
    body, head, name = map(_squash, (raw_body, raw_head, raw_name))
    scores: list[tuple[str, float, list[str]]] = []
    for doc_type in dict.fromkeys(chain(config.body, ("protocol",) if config.structure else ())):
        score = 0.0
        hits: list[str] = []
        for expression in config.body.get(doc_type, ()):
            word, weight = expression.value, expression.weight
            if expression.is_regex:
                n = expression.count(raw_body)
                in_head = n and expression.matches(raw_head)
                in_name = expression.matches(raw_name)
            else:
                normalized = _squash(word)
                if not normalized:
                    continue
                n = min(body.count(normalized), 3)
                in_head = n and normalized in head
                in_name = normalized in name
            if n:
                score += weight * n ** 0.5
                if in_head:
                    score += weight * 2  # 제목 영역 가산
                hits.append(word)
            if in_name:
                score += weight * 1.5
                hits.append(f"파일명:{word}")
        if doc_type == "protocol":
            structure = sum(_structure_match(expression, raw_body, body) for expression in config.structure)
            if structure >= 4:
                score += structure * 1.5
                hits.append(f"계획서 구성 {structure}개 항목")
        if score:
            scores.append((doc_type, round(score, 2), hits))
    scores.sort(key=lambda x: -x[1])
    return scores


def _structure_match(expression: Expression, raw: str, normalized: str) -> bool:
    if expression.is_regex:
        return expression.matches(raw)
    return _squash(expression.value) in normalized


def classify_document(filename: str, text: str, *,
                      patterns: ClassificationPatterns | None = None) -> tuple[str, float, str]:
    """(서류 종류, 신뢰도 0~1, 근거)."""
    if not text.strip():
        return "other", 0.0, "텍스트가 없어 분류할 수 없음"
    scores = score_document(filename, text, patterns=patterns)
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
