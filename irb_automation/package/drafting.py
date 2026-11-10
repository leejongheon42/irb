"""서술형 항목 초안 작성: 비어 있는 과제 항목(배경·방법·대상자 수 근거·보안 대책·동의 면제 사유 등)을 채운다.

- 규칙 초안: 과제에 입력한 선택값(자료원·처리 수준·보관 기간·면제 사유 등)으로 표준 문안을 만든다(항상 동작).
- AI 초안(선택): 제목·목적·선택값을 바탕으로 Claude가 문장을 쓴다. 개인정보는 마스킹 후 전송한다.
이미 내용이 있는 항목은 건드리지 않는다. 초안은 반드시 연구자가 검토해야 한다.
"""
from __future__ import annotations

import logging
from typing import Any

from ..engine.ruleset import get_ruleset
from ..models import Protocol

log = logging.getLogger(__name__)

FIELDS: dict[str, str] = {
    "objectives": "연구 목적",
    "background": "연구 배경 및 필요성",
    "methods": "연구 방법",
    "target_population": "연구 대상",
    "inclusion_criteria": "선정 기준",
    "exclusion_criteria": "제외 기준",
    "sample_size_justification": "대상자 수 산출 근거",
    "identifier_justification": "식별정보 수집 사유",
    "access_controls": "접근 통제·보안 대책",
    "destruction_method": "자료 보관·파기 방법",
    "waiver_impracticable_detail": "동의 면제 - 동의 취득 곤란 사유",
    "waiver_minimal_risk_detail": "동의 면제 - 최소 위험 근거",
}
MIN_LEN = 15  # 이보다 짧으면 비어 있는 것으로 본다


def empty_fields(p: Protocol) -> list[str]:
    out = []
    for f in FIELDS:
        if f.startswith("waiver_") and p.consent_plan != "waiver":
            continue
        if f == "identifier_justification" and not p.identifiers_collected:
            continue
        if len((getattr(p, f) or "").strip()) < MIN_LEN:
            out.append(f)
    return out


# ------------------------------------------------------------------ 규칙 초안
def rule_drafts(p: Protocol, fields: list[str]) -> dict[str, str]:
    rs = get_ruleset()
    sources = ", ".join(rs.label("data_sources", s) for s in (p.data_sources or [])) or "의무기록"
    period = (f"{p.data_period_start or '?'} ~ {p.data_period_end or '?'}" if (p.data_period_start or p.data_period_end)
              else "연구계획서에 정한 기간")
    deid = rs.label("deid_levels", p.deid_level)
    store = rs.label("storage_locations", p.storage_location)
    out: dict[str, str] = {}
    for f in fields:
        if f == "sample_size_justification" and p.expected_subjects:
            out[f] = (f"{period}에 {sources}에서 선정 기준을 만족하는 대상자 전수를 분석하며, 예비 조회 결과 약 "
                      f"{p.expected_subjects:,}명이 해당될 것으로 예상된다." if p.existing_data_only else
                      f"주요 결과 변수의 효과 크기와 유의수준 5%, 검정력 80%를 가정하고 탈락률을 고려하여 총 {p.expected_subjects:,}명으로 정하였다.")
        elif f == "access_controls":
            out[f] = (f"자료는 {deid} 상태로 {store}에 보관하며, 연구진 개인 계정에만 최소 권한으로 접근을 허용한다. "
                      "외부 저장매체 반출과 인터넷 전송을 금지하고, 접근·반출 기록을 남기며, 연구 종료 후 접근 권한을 회수한다.")
        elif f == "destruction_method":
            out[f] = (f"연구 관련 기록은 연구 종료 후 {p.retention_years}년간 보관하고, 보관 기간이 지나면 전자 파일은 복구할 수 없는 "
                      "방법으로 영구 삭제하고 출력물은 분쇄하여 파기한 뒤 파기 내역을 기록한다.")
        elif f == "waiver_impracticable_detail":
            reasons = ", ".join(rs.label("waiver_reasons", r) for r in (p.waiver_impracticable_reasons or []))
            out[f] = (f"본 연구는 {period}의 {sources} 자료 약 {p.expected_subjects:,}명을 이용하는 연구로"
                      + (f" ({reasons})" if reasons else "")
                      + ", 상당수 대상자가 이미 진료를 종료하였거나 연락이 닿지 않아 개별적으로 동의를 받는 것이 현실적으로 불가능하다. "
                      "또한 동의를 받은 일부 대상자만 분석하면 선택 편향으로 연구의 타당성이 크게 훼손된다.")
        elif f == "waiver_minimal_risk_detail":
            out[f] = (f"연구대상자와 직접 접촉하지 않고 이미 생성된 자료를 {deid} 상태로만 이용하므로 신체적 위험이 없고, "
                      "개인정보 보호 대책을 적용하여 정보 노출 위험이 극히 낮다. 연구대상자가 동의를 거부할 것으로 추정할 만한 사유도 없다.")
        elif f == "identifier_justification" and p.identifiers_collected:
            ids = ", ".join(rs.label("identifiers", i) for i in p.identifiers_collected)
            out[f] = (f"{ids}는 자료 연계(추적 관찰 정보 결합)에만 사용하며, 연계 즉시 연구용 일련번호로 대체하고 원 식별정보는 "
                      "분석 자료에서 삭제한다. 대체표는 연구책임자가 분리 보관한다.")
    return out


# ------------------------------------------------------------------ AI 초안
SYSTEM = """당신은 한국 병원 연구자가 기관생명윤리위원회(IRB)에 제출할 연구계획서 항목을 작성하도록 돕습니다.
주어진 과제 정보만 근거로 각 항목의 초안을 한국어 문어체(~한다)로 2~5문장 작성하십시오.
- 사실을 지어내지 마십시오(구체적 수치·문헌·기관명은 주어진 정보에 있을 때만 사용).
- 정보가 부족한 부분은 '[확인 필요: ...]'로 남기십시오.
- 생명윤리법·개인정보 보호법 관점에서 IRB가 기대하는 내용을 빠짐없이 다루십시오."""


def ai_drafts(p: Protocol, fields: list[str], db=None, user=None) -> tuple[dict[str, str], str]:
    """(초안, 상태 메시지). AI가 꺼져 있거나 실패하면 빈 dict."""
    try:
        from ..ai import client as ai
        from ..ai.privacy import mask_text, record_transmission
    except ImportError:  # pragma: no cover
        return {}, "AI 모듈 없음"
    if not fields:
        return {}, "작성할 항목 없음"
    if not ai.ai_enabled():
        return {}, "AI 꺼짐(규칙 초안만 작성)"
    rs = get_ruleset()
    info = "\n".join(f"- {k}: {v}" for k, v in {
        "제목": p.title, "연구 유형": rs.label("study_types", p.study_type), "목적": p.objectives,
        "배경": p.background, "방법": p.methods, "대상": p.target_population, "선정 기준": p.inclusion_criteria,
        "제외 기준": p.exclusion_criteria, "대상자 수": f"{p.expected_subjects:,}명",
        "자료원": ", ".join(rs.label("data_sources", s) for s in p.data_sources or []),
        "자료 기간": f"{p.data_period_start} ~ {p.data_period_end}", "수집 항목": p.data_items.replace("\n", ", "),
        "처리 수준": rs.label("deid_levels", p.deid_level), "동의": p.consent_plan,
        "취약 대상": ", ".join(p.vulnerable_populations or []) or "없음",
    }.items() if v)
    g = mask_text(info)
    schema = {"type": "object", "properties": {f: {"type": "string"} for f in fields},
              "required": fields, "additionalProperties": False}
    ask = "작성할 항목:\n" + "\n".join(f"- {f}: {FIELDS[f]}" for f in fields)
    try:
        data = ai.structured_call(system=SYSTEM, content=f"과제 정보:\n{g.text}\n\n{ask}", schema=schema,
                                  effort="medium", max_tokens=8000, purpose="연구계획서 초안 작성")
    except ai.AIError as e:
        return {}, f"AI 초안 실패: {e}"
    if db is not None:
        record_transmission(db, user, "연구계획서 초안 작성", "protocol", p.id, g.text, "text", g.masked)
    return {f: str(data.get(f, "")).strip() for f in fields if str(data.get(f, "")).strip()}, "AI 초안 작성"


def draft(p: Protocol, fields: list[str] | None = None, *, use_ai: bool = True, db=None, user=None) -> dict[str, Any]:
    """비어 있는 항목의 초안 {field: {text, source}}과 메시지."""
    fields = fields or empty_fields(p)
    out: dict[str, dict[str, str]] = {}
    msg = "AI 미사용"
    if use_ai:
        ai_out, msg = ai_drafts(p, fields, db=db, user=user)
        out.update({k: {"text": v, "source": "ai"} for k, v in ai_out.items()})
    for k, v in rule_drafts(p, [f for f in fields if f not in out]).items():
        out[k] = {"text": v, "source": "rule"}
    return {"drafts": out, "message": msg, "missing": [f for f in fields if f not in out]}
