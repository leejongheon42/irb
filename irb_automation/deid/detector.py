"""컬럼 단위 개인식별정보 자동 탐지.

헤더명 규칙과 값 패턴을 함께 사용해 각 컬럼을 직접식별자/준식별자/민감속성/자유기술로 분류하고
가명처리 기법을 권고한다. 화면에는 원본 값이 아닌 마스킹된 예시만 노출한다.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from . import patterns as P

SAMPLE_LIMIT = 2000  # 값 패턴 분석에 사용할 최대 행 수


def mask_value(value: str) -> str:
    v = value.strip()
    if not v:
        return ""
    if len(v) <= 2:
        return v[0] + "*"
    if len(v) <= 4:
        return v[0] + "*" * (len(v) - 1)
    keep = max(1, len(v) // 4)
    return v[:keep] + "*" * (len(v) - 2 * keep) + v[-keep:]


def _ratio(values: list[str], pred) -> float:
    if not values:
        return 0.0
    return sum(1 for v in values if pred(v)) / len(values)


def _is_number(v: str) -> bool:
    try:
        float(v.replace(",", ""))
        return True
    except ValueError:
        return False


def scan_text_pii(values: list[str]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for v in values:
        for kind, pat, _ in P.TEXT_SCRUB_PATTERNS:
            n = len(pat.findall(v))
            if n:
                counts[kind] += n
    return dict(counts)


def analyze_column(name: str, raw_values: list[str]) -> dict[str, Any]:
    values = [v.strip() for v in raw_values[:SAMPLE_LIMIT] if v is not None and v.strip()]
    total = len(raw_values[:SAMPLE_LIMIT]) or 1
    distinct = len(set(values))
    avg_len = (sum(len(v) for v in values) / len(values)) if values else 0.0

    evidence: list[str] = []
    detected, category, action, confidence = "unknown", "other", "keep", 0.3

    # 1) 헤더 규칙
    for dtype, cat, regex, act in P.HEADER_RULES:
        if regex.search(name):
            if dtype == "free_text" and avg_len < 15:
                continue
            detected, category, action, confidence = dtype, cat, act, 0.7
            evidence.append("헤더명 일치")
            break

    # 2) 값 패턴
    value_checks: list[tuple[str, str, str, Any]] = [
        ("rrn", "direct", "delete", lambda v: bool(P.RRN.fullmatch(v.replace(" ", "")))),
        ("phone", "direct", "delete", lambda v: bool(P.MOBILE.fullmatch(v) or P.LANDLINE.fullmatch(v))),
        ("email", "direct", "delete", lambda v: bool(P.EMAIL.fullmatch(v))),
        ("passport", "direct", "delete", lambda v: bool(P.PASSPORT.fullmatch(v))),
        ("account_no", "direct", "delete", lambda v: bool(P.CARD.fullmatch(v))),
        ("ip", "direct", "delete", lambda v: bool(P.IPV4.fullmatch(v))),
        ("date", "quasi", "date_shift", lambda v: bool(P.DATE.fullmatch(v) or P.DATE_COMPACT.match(v))),
        ("address", "quasi", "address_generalize", lambda v: bool(P.ADDRESS.search(v))),
    ]
    for dtype, cat, act, pred in value_checks:
        r = _ratio(values, pred)
        if r < 0.6:
            continue
        evidence.append(f"값 패턴({P.TYPE_LABELS.get(dtype, dtype)}) {r:.0%} 일치")
        confidence = max(confidence, min(0.99, 0.5 + r / 2))
        # 헤더로 분류되지 않았거나, 값이 직접식별자임을 보여주면 값 기준으로 분류를 상향
        if detected == "unknown" or (cat == "direct" and category != "direct"):
            detected, category, action = dtype, cat, act
        break

    # 3) 이름 추정 (헤더 미일치 시 고유값 비율이 높고 이름 형태인 경우)
    if detected == "unknown" and values:
        name_ratio = _ratio(values, P.looks_like_korean_name)
        if name_ratio >= 0.8 and distinct / len(values) >= 0.5:
            detected, category, action, confidence = "name", "direct", "delete", 0.6 + name_ratio / 4
            evidence.append(f"한글 성명 형태 {name_ratio:.0%}")

    # 4) 자유기술 내 개인정보
    pii_in_text: dict[str, int] = {}
    if avg_len >= 15 or detected == "free_text":
        pii_in_text = scan_text_pii(values)
        if detected == "unknown" and avg_len >= 30:
            detected, category, action, confidence = "free_text", "free_text", "text_scrub", 0.6
            evidence.append(f"평균 길이 {avg_len:.0f}자")
        if pii_in_text:
            evidence.append("텍스트 내 개인정보: " + ", ".join(f"{k} {v}건" for k, v in pii_in_text.items()))
            # 주소·날짜 등 이미 유형이 확인된 컬럼은 유지하고, 일반 속성일 때만 자유기술로 전환
            if category in ("other", "free_text"):
                detected, category, action = "free_text", "free_text", "text_scrub"
                confidence = max(confidence, 0.8)

    # 5) 일반 속성 타입 추정
    if detected == "unknown" and values:
        if _ratio(values, _is_number) >= 0.9:
            detected = "numeric"
        else:
            detected = "categorical"
        confidence = 0.5

    # 고유값이 많은 식별자형 코드(예: 미지정 환자 ID) 경고
    high_cardinality = bool(values) and distinct / len(values) >= 0.95 and len(values) >= 20
    if detected in ("categorical",) and high_cardinality and avg_len <= 20:
        evidence.append("고유값 비율이 높아 식별자일 가능성")
        detected, category, action, confidence = "unknown", "direct", "pseudonymize_hmac", 0.5

    samples = [mask_value(v) for v in values[:3]] if category in ("direct", "free_text", "quasi") else values[:3]
    if category == "free_text":
        samples = [mask_value(v[:40]) for v in values[:2]]

    return {
        "name": name,
        "detected_type": detected,
        "detected_label": P.TYPE_LABELS.get(detected, detected),
        "category": category,
        "category_label": P.CATEGORY_LABELS[category],
        "suggested_action": action,
        "confidence": round(confidence, 2),
        "evidence": evidence,
        "null_ratio": round(1 - len(values) / total, 3),
        "distinct_count": distinct,
        "avg_length": round(avg_len, 1),
        "pii_in_text": pii_in_text,
        "samples": samples,
    }


def analyze_table(headers: list[str], rows: list[list[str]]) -> dict[str, Any]:
    columns = []
    for idx, h in enumerate(headers):
        col_values = [r[idx] if idx < len(r) else "" for r in rows]
        columns.append(analyze_column(h, col_values))
    counts = Counter(c["category"] for c in columns)
    return {
        "row_count": len(rows),
        "column_count": len(headers),
        "columns": columns,
        "category_counts": dict(counts),
        "direct_identifiers": [c["name"] for c in columns if c["category"] == "direct"],
        "quasi_identifiers": [c["name"] for c in columns if c["category"] == "quasi"],
        "sensitive_attributes": [c["name"] for c in columns if c["category"] == "sensitive"],
    }
