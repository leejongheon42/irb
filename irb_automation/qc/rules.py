"""Rule registry and legacy code mapping."""
from functools import lru_cache
from pathlib import Path
from typing import NotRequired, TypedDict

import yaml


class Flag(TypedDict, total=False):
    severity: str | None
    source: str
    escalate: str


class Rule(TypedDict, total=False):
    label: str
    source: str
    flags: dict[str, Flag]


class Registry(TypedDict):
    version: str
    rules: dict[str, Rule]
    compatible_fields: NotRequired[dict[str, list[str]]]
    detectors: NotRequired[dict[str, dict[str, list[str]]]]


_PATH = Path(__file__).with_name("rules.yaml")
_CROSS = {
    "subjects": ("E02", "대상자 수"),
    "dosage": ("M03", "용량 불일치"),
    "dose_freq": ("M03", "빈도 불일치"),
    "dose_route": ("M03", "경로 불일치"),
    "visits": ("M04", "방문 횟수 불일치"),
    "duration": ("M05", "기간 길이 불일치"),
    "study_period": ("M05", "기간 날짜 불일치"),
    "data_period": ("M05", "기간 날짜 불일치"),
    "drug": ("M02", "약물 정체성 불일치"),
    "retention": ("E07", "보관·폐기"),
    "pi": ("E01", "연구·기관 식별"),
    "title": ("E01", "연구·기관 식별"),
    "study_no": ("E01", "연구·기관 식별"),
    "site": ("E01", "연구·기관 식별"),
    "age_range": ("E03", "선정·제외 기준"),
    "sample_volume": ("E05", "수행 절차·검체"),
    "deid_level": ("E06", "개인정보·이용·제공"),
    "expense": ("E08", "비용·보상"),
    "contact": ("E09", "문의처"),
}

_EXACT = {
    "VERSION_MISMATCH": ("M01", "파일명–본문 버전 불일치"),
    "VERSION_INCONSISTENT": ("M01", "문서 내부 버전 불일치"),
    "VERSION_MISSING": ("M01", "현재 버전 식별정보 누락"),
    "RETENTION_SHORT": ("M10", "적용 기준 불충족"),
    "CONSENT_PLAN_MISMATCH": ("E04", "동의 경로"),
    "TYPO": ("M08", "일반 오탈자"),
    "TYPO_DUP": ("M08", "단어 중복·탈락"),
    "DATE_INVALID": ("M09", "날짜 값 오류"),
    "RANGE_REVERSED": ("M09", "날짜 순서 오류"),
    "SUBJECTS_SUM": ("E02", "대상자 수"),
    "DOSE_SUM": ("M03", "용량–총량 모순"),
}


@lru_cache(maxsize=4)
def _load(mtime: int) -> Registry:
    return yaml.safe_load(_PATH.read_text(encoding="utf-8"))


def rules() -> Registry:
    return _load(_PATH.stat().st_mtime_ns)


def flag_meta(rule_id: str, flag: str) -> Flag:
    return rules()["rules"].get(rule_id, {}).get("flags", {}).get(flag, {})


def detector_rows(ai_used: bool) -> list[tuple[str, str, str]]:
    registry = rules()
    detectors = registry.get("detectors", {})
    return [(rid, flag, "규칙" if flag in detectors.get(rid, {}).get("rule", [])
             else "AI 분석 대상" if ai_used and flag in detectors.get(rid, {}).get("ai", []) else "미점검")
            for rid, rule in registry["rules"].items() for flag in rule.get("flags", {})]


def cross_metadata(key: str) -> tuple[str, str, str]:
    rule, flag = _CROSS[key]
    return rule, flag, flag_meta(rule, flag).get("severity") or "major"


def legacy_flag(code: str, status: str = "") -> tuple[str, str]:
    if code.startswith("RECORD_"):
        return "R00", "과제 입력값 불일치"
    if code.startswith("REQ_"):
        if code.endswith("_VOLUNTARY"):
            return "M06", "철회 권리 누락"
        return "M06", "필수항목 불완전" if status == "partial" else "필수항목 누락"
    if code.startswith("TERM_"):
        return "M07", "동일 개념 명칭 혼용"
    if code.startswith("SELF_"):
        return "M09", "반복 수치 불일치"
    return _EXACT.get(code, ("", ""))
