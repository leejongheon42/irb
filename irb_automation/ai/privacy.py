"""AI 전송 전 개인정보 가드.

- 텍스트: deid.patterns(주민등록번호·전화·이메일·주소·카드·여권·IP·'홍길동 환자'형 이름을 탐지해 마스킹한 뒤 전송
  (연구 기간 등 날짜는 정보 추출에 필요하므로 보존한다)
- 원본 바이너리(PDF·이미지): 정책상 허용된 서류 종류이고, 추출 텍스트에서 개인정보가 탐지되지 않을 때만 전송
- 전송 사실(목적·대상 ID·SHA-256·크기·마스킹 건수)만 감사 로그에 남기고 내용은 남기지 않는다
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..deid import patterns as P

# 날짜는 개인식별정보가 아니라 연구 정보(기간)로 취급해 탐지·마스킹에서 제외한다.
# 연구 문서에는 '신장병 환자', '환자로 1년' 같은 표현이 많아 일반 비식별 패턴을 그대로 쓰면 오탐이 많으므로
# 이름·도로명 주소는 아래 검증을 한 번 더 거친다.
_SCRUB = [(k, rx, tag) for k, rx, tag in P.TEXT_SCRUB_PATTERNS if k != "date"]
_DISEASE_SUFFIX = re.compile(r"(병|증|염|암|종|질환|장애|천식|당뇨|결핵|손상|골절|감염|중독|증후군|대상|입원|외래|소아|성인|노인|여성|남성|임산부|투석|수술|중증|경증|말기|초기|신규|기존|해당|일반|전체|모든|대조군|실험군|군)$")
_UNIT_AFTER = re.compile(r"^\s?\d+(?:-\d+)?\s*(년|개월|월|일|세|명|례|회|차|%|주|시간|분|mg|kg|cm|배|건|개|종|기)")


def _valid(kind: str, m: re.Match[str]) -> bool:
    """도로명 주소 패턴('…로 12')의 오탐 제거: 조사 '로' + 숫자·단위('환자로 1년', '기준으로 3개월')."""
    if kind != "address" or m.re is not P.ADDRESS_DETAIL:
        return True
    token = m.group(0)
    if re.search(r"(아파트|빌라|오피스텔)$|\d+동\s?\d+호$", token):
        return True
    word = re.match(r"[가-힣0-9]+?(?=(로|길)\s?\d)", token)
    if word is None or len(word.group(0)) < 2:
        return False
    if re.search(r"(자|기|준|으|별|도|위|부|간|후|전|상|하|중|적|과|만)$", word.group(0)):
        return False
    after = m.string[m.start() + len(word.group(0)) + 1:]
    return not _UNIT_AFTER.match(after)


def _name_hits(text: str, mask: bool) -> tuple[str, int]:
    """호칭형 이름('홍길동 환자', '김철수님') - 질병·집단 명칭은 제외."""
    from ..deid.transforms import _HONORIFIC_NAME, _NAME_STOPWORDS

    hits = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal hits
        word = m[1]
        if word in _NAME_STOPWORDS or _DISEASE_SUFFIX.search(word) or not P.looks_like_korean_name(word):
            return m[0]
        hits += 1
        return ("[이름]" + m[2] + m[3]) if mask else m[0]

    return _HONORIFIC_NAME.sub(repl, text), hits


def _process(text: str, mask: bool) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for kind, rx, tag in _SCRUB:
        def repl(m: re.Match[str], kind=kind, tag=tag) -> str:
            if not _valid(kind, m):
                return m.group(0)
            counts[kind] = counts.get(kind, 0) + 1
            return tag if mask else m.group(0)

        text = rx.sub(repl, text)
    text, names = _name_hits(text, mask)
    if names:
        counts["name"] = names
    return text, counts


@dataclass
class GuardResult:
    text: str
    pii: dict[str, int] = field(default_factory=dict)
    masked: int = 0

    @property
    def has_pii(self) -> bool:
        return bool(self.pii)


def scan(text: str) -> dict[str, int]:
    """텍스트의 개인정보 패턴 건수(날짜 제외, 연구 문서 오탐 보정)."""
    return _process(text, mask=False)[1]


def mask_text(text: str) -> GuardResult:
    """날짜는 보존하고 개인정보를 마스킹한 텍스트."""
    masked, counts = _process(text, mask=True)
    return GuardResult(text=masked, pii=counts, masked=sum(counts.values()))


def binary_allowed(doc_type: str | None, extracted_text: str) -> tuple[bool, str]:
    """원본 파일을 AI로 보낼 수 있는지 판단."""
    allowed = get_settings().ai_doc_types_allowed
    if not doc_type or doc_type not in allowed:
        return False, f"원본 전송 허용 서류가 아님({doc_type or '미분류'}; 허용: {', '.join(allowed)})"
    if extracted_text.strip():
        pii = scan(extracted_text)
        if pii:
            return False, "문서에서 개인정보 패턴이 탐지되어 원본을 전송하지 않음: " + ", ".join(f"{k} {v}건" for k, v in pii.items())
        return True, "허용 서류 · 개인정보 미탐지"
    return True, "허용 서류 · 텍스트층이 없어 개인정보 사전 점검 불가(스캔본)"


def record_transmission(
    db: Session,
    user: Any,
    purpose: str,
    entity_type: str,
    entity_id: int | None,
    payload: bytes | str,
    kind: str,
    masked: int = 0,
) -> None:
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    audit.record(
        db, user, "ai_transmit", entity_type, entity_id,
        {
            "purpose": purpose,
            "kind": kind,  # text | pdf | image
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "masked": masked,
            "model": get_settings().ai_model,
        },
    )
