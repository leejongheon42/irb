"""자리표시자 이름 → 과제 필드 자동 매핑 추천.

1) 필드 경로·표시명 일치  2) 한글 별칭 사전  3) 문자열 유사도 순으로 추천한다.
병원마다 '연구제목/과제명/연구과제명'처럼 표현이 달라도 대부분 자동으로 연결된다.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from .fields import FieldInfo

ALIASES: dict[str, list[str]] = {
    "title": ["연구제목", "연구과제명", "과제명", "연구명", "제목", "연구과제", "과제제목", "국문제목", "연구과제제목"],
    "protocol_no": ["과제번호", "접수번호", "IRB번호", "심의번호", "승인번호", "연구번호"],
    "version": ["버전", "판", "개정번호", "계획서버전"],
    "department": ["진료과", "부서", "소속과"],
    "study_type": ["연구유형", "연구종류", "연구형태", "연구설계"],
    "funding_text": ["연구비", "연구비지원", "연구비지원기관", "지원기관", "연구비출처", "재원"],
    "study_period": ["연구기간", "총연구기간", "연구수행기간"],
    "study_start": ["연구시작일", "시작일", "연구개시일"],
    "study_end": ["연구종료일", "종료일", "연구완료일"],
    "pi_name": ["연구책임자", "책임연구자", "연구책임자성명", "책임자", "연구책임자명", "성명", "신청자"],
    "pi_department": ["소속", "연구책임자소속", "책임자소속", "소속기관", "소속부서"],
    "pi_email": ["연락처", "이메일", "전자우편", "연구책임자연락처", "email"],
    "pi_training_expires": ["교육이수", "교육이수일", "교육만료일", "연구윤리교육"],
    "co_investigators": ["공동연구자", "공동연구원", "연구참여자", "연구원"],
    "team": ["연구진", "연구팀", "연구진목록", "연구참여자목록", "연구원목록"],
    "team_names": ["연구진성명"],
    "objectives": ["연구목적", "목적", "연구목표", "연구의목적"],
    "background": ["연구배경", "배경", "연구의배경", "배경및필요성"],
    "methods": ["연구방법", "방법", "연구의방법", "연구설계및방법"],
    "target_population": ["연구대상자", "대상자", "연구대상"],
    "inclusion_criteria": ["선정기준", "포함기준", "대상자선정기준"],
    "exclusion_criteria": ["제외기준", "대상자제외기준"],
    "expected_subjects_text": ["대상자수", "예상대상자수", "목표대상자수", "연구대상자수", "목표인원", "대상자규모"],
    "sample_size_justification": ["대상자수산출근거", "산출근거", "표본수산출근거"],
    "data_sources": ["자료출처", "데이터출처", "자료원"],
    "data_period": ["자료수집기간", "자료기간", "자료대상기간", "의무기록조회기간", "조사대상기간"],
    "data_items": ["수집항목", "수집변수", "자료수집항목", "조사항목", "변수목록"],
    "data_item_rows": ["수집항목표", "변수표"],
    "identifiers_label": ["식별정보", "수집식별자", "개인식별정보", "제공식별자"],
    "identifier_rows": ["식별자표"],
    "identifier_justification": ["식별자수집사유", "식별정보수집사유"],
    "sensitive_label": ["민감정보"],
    "vulnerable_label": ["취약한연구대상자", "취약대상자", "취약계층"],
    "deid_level": ["개인정보처리수준", "가명처리수준", "비식별수준", "처리수준", "가명처리여부"],
    "storage_location": ["보관장소", "자료보관장소", "분석장소"],
    "access_controls": ["안전조치", "보안대책", "개인정보보호대책", "보안관리방안"],
    "external_recipient": ["제공받는기관", "자료제공기관", "반출기관"],
    "retention_text": ["보관기간", "자료보관기간"],
    "destruction_method": ["파기방법", "자료파기방법", "폐기방법"],
    "consent_plan": ["동의방식", "동의취득방법", "동의여부", "동의"],
    "consent_text": ["동의방법", "동의취득"],
    "waiver_impracticable_detail": ["동의면제사유", "면제사유", "동의면제사유설명"],
    "waiver_minimal_risk_detail": ["위험근거", "최소위험근거", "위험이극히낮은근거"],
    "coi_text": ["이해상충", "이해관계", "이해상충여부"],
    "coi_detail": ["이해상충내용"],
    "participating_sites": ["참여기관", "공동연구기관", "다기관목록"],
    "external_irb": ["대표IRB", "주관IRB", "심의IRB"],
    "review_type_label": ["심의유형", "심의구분", "심의종류"],
    "risk_level_label": ["위험도", "위험수준"],
    "auto.review_type_label": ["권고심의유형"],
    "approved_at": ["승인일", "승인일자"],
    "expires_at": ["만료일", "승인만료일", "유효기간"],
    "today": ["작성일", "신청일", "제출일", "일자", "날짜", "작성일자", "신청일자"],
    "year": ["연도", "년도"],
    "profile.institution_name": ["기관명", "병원명", "의료기관명", "기관"],
    "profile.irb_name": ["위원회명", "IRB명", "기관위원회명", "위원회"],
    "profile.contact": ["사무국연락처", "IRB연락처", "문의처"],
}

_PUNCT = re.compile(r"[\s\-_·.,/()\[\]{}<>「」『』:;'\"]+")


def norm(text: str) -> str:
    return _PUNCT.sub("", text or "").lower()


ALIAS_INDEX: dict[str, str] = {norm(alias): path for path, aliases in ALIASES.items() for alias in aliases}


def suggest(name: str, kind: str, fields: list[FieldInfo]) -> dict[str, Any] | None:
    """{"path", "score", "reason"} 또는 None."""
    candidates = [f for f in fields if not f.path.startswith("__")]
    if kind == "list":
        candidates = [f for f in candidates if f.kind == "list"]
    by_path = {f.path: f for f in candidates}
    n = norm(name)
    if not n:
        return None
    for f in candidates:
        if norm(f.path) == n:
            return {"path": f.path, "score": 1.0, "reason": "필드명 일치"}
    path = ALIAS_INDEX.get(n)  # 기관 서식 관행을 반영한 별칭 사전을 표시명보다 우선
    if path and path in by_path:
        return {"path": path, "score": 0.95, "reason": "별칭 사전"}
    for f in candidates:
        if norm(f.label) == n:
            return {"path": f.path, "score": 0.9, "reason": "표시명 일치"}
    best: tuple[float, str] = (0.0, "")
    pool = [(norm(f.label), f.path) for f in candidates]
    pool += [(alias, p) for alias, p in ALIAS_INDEX.items() if p in by_path]
    for cand, cand_path in pool:
        if not cand:
            continue
        score = SequenceMatcher(None, n, cand).ratio()
        if len(n) >= 2 and len(cand) >= 2 and (n in cand or cand in n):
            score = max(score, 0.8)
        if score > best[0]:
            best = (score, cand_path)
    if best[0] >= 0.72:
        return {"path": best[1], "score": round(best[0], 2), "reason": "유사도"}
    return None


def suggest_all(placeholders: list[dict[str, Any]], fields: list[FieldInfo]) -> dict[str, dict[str, Any]]:
    out = {}
    for ph in placeholders:
        hit = suggest(ph["name"], ph.get("kind", "field"), fields)
        if hit:
            out[ph["name"]] = hit
    return out
