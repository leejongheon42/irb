"""Phase 2: AI(Claude) 심층 분석 - 문맥을 이해해야 잡히는 교차 참조 불일치·자기모순·규제 비준수·가독성 문제.

규칙 엔진이 이미 찾은 이슈 목록을 함께 보내 중복 지적을 피한다. 문서 텍스트는 개인정보를 마스킹한 뒤 보내며,
AI가 제안한 교정(old→new)은 old가 실제 문서에 그대로 있을 때만 자동 교정 대상으로 채택한다.
"""
from __future__ import annotations

import logging
from typing import Any

from .checks import QCDoc, criteria

log = logging.getLogger(__name__)

MAX_TOTAL_CHARS = 600_000  # 이 이상은 잘라 보내지 않고 AI 단계를 건너뛴다(부분 점검 오해 방지)

SYSTEM = """당신은 한국 병원 기관생명윤리위원회(IRB)에 제출할 연구 서류를 제출 전에 점검하는 품질 검토자입니다.
연구자가 IRB 보완 요청을 받지 않도록, 심의위원이 지적할 만한 문제를 찾아냅니다.

점검 관점:
- 문서 간 교차 참조 불일치(계획서·동의서·신청서 사이의 대상자 수, 기간, 절차, 방문, 수집 항목, 보관 기간, 위험 설명 등)
- 한 문서 안의 자기모순(앞에서는 익명, 뒤에서는 등록번호 수집 등)
- 생명윤리법·개인정보 보호법·ICH-GCP 관점의 누락이나 부적절한 표현(동의 면제 요건 소명 부족, 가명정보 처리 근거 등)
- 연구대상자가 이해하기 어려운 표현(동의서의 전문용어 남용, 지나치게 긴 문장)
- 의학 용어·약물명·용량·경로의 오기

규칙:
- 이미 찾은 이슈(아래 목록)와 같은 지적은 하지 마십시오.
- 근거가 문서에 있는 문제만 지적하고, 추측은 하지 마십시오. evidence에는 문서 원문을 그대로 인용하십시오.
- 고칠 문구가 명확하면 fix_old(문서 원문 그대로, 15자 이상 권장)와 fix_new를 주고, 내용을 새로 써야 하는 경우는 비워 두십시오.
- severity: critical(IRB 반려·연구대상자 안전·법 위반 소지), major(보완 요청 가능성 높음), minor(개선 권고).
- 문제가 없으면 issues를 빈 배열로 두십시오."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "doc_index": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["critical", "major", "minor"]},
                    "category": {"type": "string", "enum": ["cross", "regulatory", "context", "typo", "numbers", "terminology"]},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                    "evidence": {"type": "string"},
                    "suggestion": {"type": "string"},
                    "fix_old": {"type": "string"},
                    "fix_new": {"type": "string"},
                },
                "required": ["doc_index", "severity", "category", "title", "detail", "evidence", "suggestion",
                             "fix_old", "fix_new"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["issues", "summary"],
    "additionalProperties": False,
}


def run_ai(docs: list[QCDoc], found: list[dict], protocol_summary: str = "", db=None, user=None,
           entity_id: int | None = None) -> tuple[list[dict], dict[str, Any]]:
    """(AI 이슈 목록, 단계 정보). AI가 꺼져 있거나 실패하면 빈 목록과 사유를 돌려준다."""
    try:
        from ..ai import client as ai
        from ..ai.privacy import mask_text, record_transmission
    except ImportError:  # pragma: no cover
        return [], {"status": "skipped", "message": "AI 모듈이 없습니다."}
    if not ai.ai_enabled():
        return [], {"status": "skipped", "message": "AI 기능이 꺼져 있어 규칙 기반 점검만 수행했습니다(IRB_AI_ENABLED)."}
    readable = [d for d in docs if d.text.strip()]
    if not readable:
        return [], {"status": "skipped", "message": "AI로 분석할 텍스트가 없습니다."}
    if sum(len(d.text) for d in readable) > MAX_TOTAL_CHARS:
        return [], {"status": "skipped", "message": "문서 분량이 커서 AI 심층 분석을 건너뛰었습니다(규칙 기반 점검은 완료). 서류를 나눠 점검하십시오."}
    masked_total = 0
    blocks: list[dict] = []
    masked_texts: dict[str, str] = {}
    for i, d in enumerate(readable):
        g = mask_text(d.text)
        masked_total += g.masked
        masked_texts[d.key] = g.text
        blocks.append(ai.text_document_block(g.text, f"[{i}] {d.label} - {d.name}"))
    cats = criteria().get("categories", {})
    known = "\n".join(f"- [{x['severity']}] {cats.get(x['category'], x['category'])}: {x['title']} ({x['doc_name']})"
                      for x in found[:80]) or "- (없음)"
    intro = (f"과제 정보 요약:\n{protocol_summary or '(없음)'}\n\n이미 찾은 이슈:\n{known}\n\n"
             f"위 {len(readable)}개 문서(doc_index는 제목의 [번호])를 점검하십시오.")
    payload = intro + "\n".join(masked_texts.values())
    try:
        data = ai.structured_call(system=SYSTEM, content=[*blocks, {"type": "text", "text": intro}], schema=SCHEMA,
                                  effort="high", max_tokens=32000, purpose="서류 품질 점검")
    except ai.AIError as e:
        log.info("qc ai phase failed: %s", e)
        return [], {"status": "failed", "message": f"AI 심층 분석 실패: {e} (규칙 기반 결과는 유효)"}
    if db is not None:
        record_transmission(db, user, "서류 품질 점검", "qc_run", entity_id, payload, "text", masked_total)
    out = []
    for it in data.get("issues", []):
        idx = it.get("doc_index", -1)
        doc = readable[idx] if 0 <= idx < len(readable) else None
        fixes = []
        old, new = (it.get("fix_old") or "").strip(), (it.get("fix_new") or "").strip()
        if doc and old and new and old != new and old in doc.text:
            fixes = [{"doc": doc.key, "old": old, "new": new}]
        out.append({
            "severity": it["severity"], "category": "context" if it["category"] == "context" else it["category"],
            "title": it["title"][:200], "detail": it.get("detail", "")[:1000], "doc": doc.key if doc else None,
            "doc_name": doc.name if doc else "", "evidence": it.get("evidence", "")[:300],
            "suggestion": it.get("suggestion", "")[:500], "fixes": fixes, "phase": 2, "source": "ai", "code": "AI",
        })
    return out, {"status": "done", "message": data.get("summary", "")[:1000], "masked": masked_total}
