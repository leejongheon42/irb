"""IRB 보완 요청 대응: 항목별 답변 초안, 수정할 과제 항목·서류 안내, 관련 근거, 보완 답변서 자동 작성.

- 규칙: 요청문 키워드로 수정 대상(과제 항목·서류)을 찾아 표준 답변 문안을 만든다.
- AI(선택): 요청문·과제 정보·관련 근거로 답변 초안과 수정 문안을 쓴다.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ..engine.ruleset import get_ruleset
from ..models import Protocol, RevisionItem

log = logging.getLogger(__name__)

# (키워드 정규식, 수정할 과제 항목, 수정할 서류)
TARGETS: list[tuple[str, list[str], list[str]]] = [
    (r"동의서|설명문|assent|법정대리인|서명", [], ["consent_form"]),
    (r"동의\s*면제|면제\s*사유", ["waiver_impracticable_detail", "waiver_minimal_risk_detail"], ["waiver_request"]),
    (r"가명|비식별|식별정보|등록번호|개인정보|보안|접근", ["identifier_justification", "access_controls", "deid_level"], ["deid_plan", "security_pledge"]),
    (r"대상자\s*수|표본|검정력|산출", ["expected_subjects", "sample_size_justification"], ["protocol"]),
    (r"선정\s*기준|제외\s*기준|연구\s*대상(?!자)", ["inclusion_criteria", "exclusion_criteria", "target_population"], ["protocol"]),
    (r"기간|일정|종료일|시작일", ["study_start", "study_end", "data_period_start", "data_period_end"], ["protocol", "consent_form"]),
    (r"보관|파기|폐기", ["retention_years", "destruction_method"], ["protocol", "deid_plan"]),
    (r"방법|설계|분석|통계", ["methods"], ["protocol"]),
    (r"수집\s*항목|변수|증례기록", ["data_items"], ["crf", "protocol"]),
    (r"이해상충|연구비|후원", ["conflict_of_interest", "coi_detail", "funding_source"], ["coi"]),
    (r"교육|이수", [], ["training_cert"]),
    (r"이력", [], ["cv"]),
    (r"외부|반출|제공|계약", ["external_transfer", "external_recipient"], ["transfer_agreement"]),
    (r"일치|불일치|다릅니다|상이", [], ["protocol", "consent_form"]),
]


def targets_for(text: str) -> tuple[list[str], list[str]]:
    fields: list[str] = []
    docs: list[str] = []
    for pat, fs, ds in TARGETS:
        if re.search(pat, text):
            fields += [f for f in fs if f not in fields]
            docs += [d for d in ds if d not in docs]
    return fields, docs


def rule_response(item: RevisionItem, docs: list[str]) -> str:
    rs = get_ruleset()
    where = ", ".join(rs.label("document_types", d) for d in docs) or "관련 서류"
    return (f"지적하신 사항을 반영하여 {where}을(를) 수정하였습니다. "
            f"[수정 내용: 구체적으로 무엇을 어떻게 고쳤는지 기재] (수정 위치: {where} 해당 항목, 변경 부분 표시)")


SYSTEM = """당신은 한국 병원 연구자가 기관생명윤리위원회(IRB)의 보완 요청에 답변서를 쓰도록 돕습니다.
각 보완 요청 항목에 대해 정중하고 구체적인 답변(2~4문장, '~하였습니다' 체)을 쓰고,
서류에 넣을 수정 문안(revised_text)을 제안하십시오.
- 과제 정보에 없는 사실은 지어내지 말고 '[확인 필요: ...]'로 남기십시오.
- 답변에는 어느 서류의 어느 부분을 고쳤는지 밝히십시오."""


def draft_responses(protocol: Protocol, items: list[RevisionItem], *, use_ai: bool = True, db=None, user=None
                    ) -> dict[int, dict[str, Any]]:
    """{item.id: {response, revised_text, fields, docs, related, source}}"""
    out: dict[int, dict[str, Any]] = {}
    related_by: dict[int, list[dict]] = {}
    try:
        from ..knowledge import service as ks

        for it in items:
            related_by[it.id] = ks.search(db, it.request_text, k=3, user=user) if db is not None else []
    except Exception:  # 지식 검색은 보조 정보
        log.exception("knowledge search for revision failed")
    for it in items:
        fields, docs = targets_for(it.request_text)
        out[it.id] = {"response": rule_response(it, docs), "revised_text": "", "fields": fields, "docs": docs,
                      "related": related_by.get(it.id, []), "source": "rule"}
    if not use_ai or not items:
        return out
    try:
        from ..ai import client as ai
        from ..ai.privacy import mask_text, record_transmission
    except ImportError:  # pragma: no cover
        return out
    if not ai.ai_enabled():
        return out
    from ..qc.service import _summary

    ctx = mask_text(_summary(protocol) + "\n목적: " + (protocol.objectives or "") + "\n방법: " + (protocol.methods or ""))
    reqs = "\n".join(f"[{it.id}] {it.request_text}" for it in items)
    schema = {"type": "object", "properties": {"answers": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "integer"}, "response": {"type": "string"},
                                         "revised_text": {"type": "string"}},
        "required": ["id", "response", "revised_text"], "additionalProperties": False}}},
        "required": ["answers"], "additionalProperties": False}
    try:
        data = ai.structured_call(system=SYSTEM, content=f"과제 정보:\n{ctx.text}\n\n보완 요청:\n{reqs}", schema=schema,
                                  effort="medium", max_tokens=8000, purpose="보완 답변 초안")
    except ai.AIError as e:
        log.info("revision ai failed: %s", e)
        return out
    if db is not None:
        record_transmission(db, user, "보완 답변 초안", "protocol", protocol.id, ctx.text + reqs, "text", ctx.masked)
    for a in data.get("answers", []):
        if a.get("id") in out and a.get("response", "").strip():
            out[a["id"]].update(response=a["response"].strip(), revised_text=a.get("revised_text", "").strip(), source="ai")
    return out
