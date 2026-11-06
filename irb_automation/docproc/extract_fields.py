"""연구계획서 정보 추출.

병원마다 다른 계획서 서식에서 우리 과제 입력 항목을 찾아낸다.

1) 규칙 기반(항상 동작)
   - 제목 변형 사전: "연구 제목/과제명/연구과제명", "의무기록 열람 기간/자료 수집 기간" 등
     (규칙 세트 document_pipeline.field_aliases 로 기관 고유 명칭 추가 가능)
   - 번호 체계: 1. / 1.2 / 1) / (1) / 가. / (가) / ① / Ⅰ. / 제n조 / 글머리표 / 마크다운 #
   - "항목: 내용" 인라인, "항목 | 내용" 표 행
   - 숫자·날짜 정규화(약 1,200명, 2018년 1월 ~ 2023년 12월, 2019.01.01-2024.12.31)
   - 키워드 추정(연구 유형·데이터 출처·식별자·민감정보·취약군, 부정 표현 고려)
2) AI 보조(선택): 구조화 출력으로 추출 후 근거 인용이 원문에 실제로 있는지 확인해 병합
"""
from __future__ import annotations

import calendar
import importlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..engine.ruleset import get_ruleset

FIELD_LABELS: dict[str, str] = {
    "title": "연구 제목",
    "objectives": "연구 목적",
    "background": "연구 배경",
    "methods": "연구 방법",
    "target_population": "연구 대상",
    "inclusion_criteria": "선정 기준",
    "exclusion_criteria": "제외 기준",
    "expected_subjects": "예상 대상자 수",
    "sample_size_justification": "대상자 수 산출 근거",
    "study_start": "연구 시작일",
    "study_end": "연구 종료일",
    "data_period_start": "자료 기간 시작",
    "data_period_end": "자료 기간 종료",
    "data_items": "수집 항목",
    "access_controls": "개인정보 보호 대책",
    "consent_plan": "동의 방식",
    "waiver_impracticable_detail": "동의 면제 사유",
    "waiver_impracticable_reasons": "동의 면제 사유(분류)",
    "retention_years": "보관 기간(년)",
    "destruction_method": "자료 보관·폐기",
    "funding_source": "연구비 지원",
    "department": "소속",
    "pi_name": "연구책임자(문서 기재)",
    "study_type": "연구 유형",
    "data_sources": "데이터 출처",
    "identifiers_collected": "수집 식별자",
    "sensitive_categories": "민감정보",
    "vulnerable_populations": "취약한 연구대상자",
    "unstructured_data": "비정형 데이터",
    "deid_level": "개인정보 처리 수준",
    "existing_data_only": "기존 자료만 이용",
    "direct_contact": "연구대상자 직접 접촉",
    "external_transfer": "기관 외 반출",
    "international_transfer": "국외 이전",
    "data_linkage": "데이터 결합",
    "ai_ml_development": "AI/ML 개발",
    "multicenter": "다기관 연구",
    "rare_disease": "희귀질환",
}

# 섹션(제목) 별칭 → 내부 섹션 키
DEFAULT_ALIASES: dict[str, list[str]] = {
    "title": ["연구제목", "연구과제명", "과제명", "연구명", "과제제목", "국문제목", "연구과제제목", "연구과제", "제목", "연구제목국문"],
    "objectives": ["연구목적", "연구의목적", "연구목표", "목적", "연구목적및필요성", "연구의목적및필요성", "목적및필요성", "연구의목표"],
    "background": ["연구배경", "연구의배경", "배경", "연구배경및필요성", "연구의배경및필요성", "필요성", "이론적배경", "선행연구", "연구의필요성"],
    "methods": ["연구방법", "연구의방법", "연구설계", "연구절차", "연구방법및절차", "연구방법및내용", "연구디자인", "자료분석방법",
                "분석방법", "통계분석방법", "통계분석", "연구내용및방법", "연구수행방법"],
    "target_population": ["연구대상", "연구대상자", "대상자", "연구대상자선정", "연구대상자모집", "대상"],
    "inclusion_criteria": ["선정기준", "포함기준", "대상자선정기준", "연구대상자선정기준", "선정조건"],
    "exclusion_criteria": ["제외기준", "배제기준", "대상자제외기준", "연구대상자제외기준", "제외조건"],
    "expected_subjects": ["목표대상자수", "예상대상자수", "연구대상자수", "대상자수", "목표인원", "예상연구대상자수", "표본수",
                          "표본크기", "목표증례수", "예상증례수", "대상자규모", "연구대상자의수", "목표연구대상자수"],
    "sample_size_justification": ["대상자수산출근거", "표본수산정근거", "표본크기산출근거", "산출근거", "대상자수산정근거", "표본수산출근거"],
    "study_period": ["연구기간", "연구수행기간", "총연구기간", "연구예정기간"],
    "data_period": ["자료수집기간", "의무기록열람기간", "자료조사기간", "열람기간", "자료기간", "조사대상기간", "자료수집대상기간",
                    "의무기록조사기간", "후향적자료수집기간", "대상기간", "연구대상기간", "자료추출기간"],
    "data_items": ["수집항목", "수집변수", "조사항목", "자료수집항목", "수집자료", "변수", "조사변수", "수집정보", "자료수집내용",
                   "연구변수", "수집하는자료"],
    "privacy": ["개인정보보호", "개인정보보호대책", "개인정보보호방안", "가명처리", "가명처리방법", "비식별화", "개인정보처리",
                "개인정보보호및비밀보장", "비밀보장", "자료보안", "개인정보보호및보안"],
    "consent": ["동의", "동의면제", "동의취득", "연구대상자동의", "서면동의", "동의서면제", "동의획득", "동의취득방법",
                "서면동의면제사유", "동의면제사유", "연구대상자동의취득"],
    "retention": ["자료보관", "자료의보관및폐기", "자료보관및폐기", "보관및폐기", "자료폐기", "보관기간", "자료의보관",
                  "자료보관및파기", "보관및파기", "연구자료보관", "자료의보관및파기"],
    "funding": ["연구비", "연구비지원", "지원기관", "연구비지원기관", "연구비출처", "연구비재원"],
    "pi_name": ["연구책임자", "책임연구자", "연구책임자성명"],
    "department": ["소속", "진료과", "소속기관및부서", "소속부서"],
}
TEXT_SECTIONS = {"title", "objectives", "background", "methods", "target_population", "inclusion_criteria",
                 "exclusion_criteria", "sample_size_justification", "data_items", "privacy", "consent", "retention",
                 "funding", "pi_name", "department"}

_PUNCT = re.compile(r"[\s·ㆍ:：()（）\[\]<>「」『』\-_/.,*\"'“”‘’]")
NUMBERING = re.compile(
    r"^\s*(?:"
    r"제\s*\d+\s*[조장절항]\s*"
    r"|[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫ]+\s*[.)]?\s*"
    r"|[IVX]{1,4}\s*[.)]\s*"
    r"|\d+(?:\.\d+)+\.?\s*"
    r"|\d+\s*[.)](?=\s|[가-힣A-Za-z])\s*"
    r"|\(\d+\)\s*"
    r"|[가-하]\s*[.)]\s*"
    r"|\([가-하]\)\s*"
    r"|[①-⑳]\s*"
    r"|[□■◎○●▶▷◆◇※•▪-]\s*"
    r")+"
)
_SENTENCE_END = re.compile(r"(다|요|음|함|임)\.?$|\.$")


def norm(s: str) -> str:
    return _PUNCT.sub("", s).lower()


def _alias_index() -> list[tuple[str, str]]:
    merged = {k: list(v) for k, v in DEFAULT_ALIASES.items()}
    extra = get_ruleset().policy("document_pipeline").get("field_aliases", {}) or {}
    for key, words in extra.items():
        merged.setdefault(key, []).extend(str(w) for w in (words or []))
    index = [(norm(a), key) for key, words in merged.items() for a in words if norm(a)]
    return sorted(set(index), key=lambda x: -len(x[0]))


@dataclass
class _Line:
    kind: str  # heading | table | content
    key: str | None = None
    conf: float = 0.0
    value: str = ""
    pairs: list[tuple[str, float, str]] | None = None


class SectionParser:
    def __init__(self) -> None:
        self.index = _alias_index()

    def match_label(self, label: str, *, structural: bool) -> tuple[str | None, float]:
        n = norm(NUMBERING.sub("", label))
        if not n or len(n) > 40:
            return None, 0.0
        for alias, key in self.index:
            if n == alias:
                return key, 0.9
        if structural or (len(label) <= 24 and not _SENTENCE_END.search(label.strip())):
            for alias, key in self.index:
                if len(alias) >= 2 and n.startswith(alias) and len(n) - len(alias) <= 8:
                    return key, 0.75
        return None, 0.0

    def parse_line(self, line: str) -> _Line:
        if " | " in line:
            cells = [c.strip() for c in line.split("|")]
            pairs = []
            i = 0
            while i < len(cells) - 1:
                key, conf = self.match_label(cells[i], structural=True)
                if key and cells[i + 1]:
                    pairs.append((key, conf, cells[i + 1]))
                    i += 2
                    continue
                i += 1
            return _Line("table", pairs=pairs)
        markdown = line.startswith("#")
        stripped = line.lstrip("#").strip()
        numbered = bool(NUMBERING.match(stripped)) and not re.match(r"^\s*[-•▪]\s", stripped)
        rest = NUMBERING.sub("", stripped).strip() if numbered else stripped
        structural = markdown or numbered
        m = re.match(r"^([^:：]{1,30})\s*[:：]\s*(.*)$", rest)
        if m and not re.match(r"^\d{1,2}$", m.group(1).strip()):
            key, conf = self.match_label(m.group(1), structural=True)
            if key:
                return _Line("heading", key, conf, m.group(2).strip())
        key, conf = self.match_label(rest, structural=structural)
        if key and len(rest) <= 40:
            return _Line("heading", key, conf)
        if structural and len(rest) <= 30 and not _SENTENCE_END.search(rest):
            return _Line("heading", None)
        return _Line("content")

    def sections(self, text: str) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        found: dict[str, list[dict[str, Any]]] = {}
        lines = [ln.strip() for ln in text.splitlines()]
        current: dict[str, Any] | None = None

        def flush() -> None:
            if current and current["key"]:
                content = "\n".join(current["lines"]).strip()
                if content:
                    found.setdefault(current["key"], []).append({
                        "value": content, "conf": current["conf"], "method": "section", "heading": current["heading"],
                        "snippet": (current["heading"] + ("" if current["heading"].endswith(content.splitlines()[0])
                                                          else " " + content))[:200],
                    })

        for line in lines:
            if not line:
                continue
            parsed = self.parse_line(line)
            if parsed.kind == "table":
                if parsed.pairs:
                    for key, conf, value in parsed.pairs:
                        found.setdefault(key, []).append(
                            {"value": value, "conf": min(0.95, conf + 0.05), "method": "table", "snippet": line[:200]}
                        )
                elif current is not None:
                    current["lines"].append(line)
                continue
            if parsed.kind == "heading":
                flush()
                current = {"key": parsed.key, "conf": parsed.conf, "heading": line,
                           "lines": [parsed.value] if parsed.value else []}
                continue
            if current is not None:
                current["lines"].append(line)
        flush()
        return found, lines


# ====================================================================== 값 정규화
_DATE_FULL = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[-./년]\s*(\d{1,2})\s*[-./월]\s*(\d{1,2})\s*일?(?!\d)")
_DATE_MONTH = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*[-./년]\s*(\d{1,2})\s*월?(?![\d.\-/])")
_DATE_YEAR = re.compile(r"(?<!\d)((?:19|20)\d{2})\s*년(?!\s*\d)")


def _date_tokens(text: str) -> list[tuple[int, date, date]]:
    """(위치, 시작 해석, 종료 해석) - 정밀도가 낮은 날짜는 구간의 첫날/마지막 날로 해석."""
    tokens: list[tuple[int, date, date]] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in taken)

    for m in _DATE_FULL.finditer(text):
        try:
            d = date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            continue
        tokens.append((m.start(), d, d))
        taken.append(m.span())
    for m in _DATE_MONTH.finditer(text):
        if not free(*m.span()):
            continue
        y, mo = int(m[1]), int(m[2])
        if 1 <= mo <= 12:
            tokens.append((m.start(), date(y, mo, 1), date(y, mo, calendar.monthrange(y, mo)[1])))
            taken.append(m.span())
    for m in _DATE_YEAR.finditer(text):
        if not free(*m.span()):
            continue
        y = int(m[1])
        tokens.append((m.start(), date(y, 1, 1), date(y, 12, 31)))
        taken.append(m.span())
    return sorted(tokens)


def parse_period(text: str) -> tuple[date | None, date | None]:
    tokens = _date_tokens(text)
    if not tokens:
        return None, None
    if len(tokens) == 1:
        pos, start, end = tokens[0]
        before = text[:pos]
        if re.search(r"승인|~|∼|부터|까지", before) or re.search(r"까지", text[pos:pos + 20]):
            return None, end
        return start, None
    return tokens[0][1], tokens[-1][2]


def parse_count(text: str) -> int | None:
    m = re.search(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)\s*(?:명|례|건|인|쌍)", text)
    if m:
        return int(m.group(1).replace(",", ""))
    for m in re.finditer(r"(?<![\d.])(\d{1,3}(?:,\d{3})+|\d+)(?![\d.,])\s*(년|월|일|세|%|개월|시간|mg)?", text):
        if not m.group(2):
            n = int(m.group(1).replace(",", ""))
            if not 1900 <= n <= 2100:
                return n
    return None


def parse_years(text: str) -> int | None:
    m = re.search(r"(\d{1,2})\s*년", text)
    if m and 0 < int(m.group(1)) <= 50:
        return int(m.group(1))
    return None


WAIVER_RE = re.compile(
    r"동의\s*(?:를|의|가)?\s*면제|면제\s*를?\s*신청|서면\s*동의\s*(?:의\s*)?면제|동의서\s*(?:취득\s*)?면제|동의\s*획득이\s*(?:현실적으로\s*)?(?:불가|어렵)"
)
OBTAIN_RE = re.compile(r"(?:동의서|서면\s*동의|동의)\s*를?\s*(?:받|취득|획득)")

WAIVER_REASON_KEYWORDS = {
    "large_population": ["대상자 수가 많", "대상자가 많", "대규모", "다수의 대상자", "대상자가 다수"],
    "deceased_or_lost": ["사망", "추적 불가", "추적이 불가", "연락이 불가", "연락 불가", "추적이 어려", "추적 관찰이 어려"],
    "selection_bias": ["선택 편향", "선택편향", "편향"],
    "outdated_contact": ["연락처가 부정확", "연락처 정보가", "주소 변경"],
    "contact_increases_risk": ["연락하는 것 자체", "노출 위험"],
}

# 키워드 추정 규칙: 키 → [(값, [패턴...])]
KEYWORD_RULES: dict[str, list[tuple[str, list[str]]]] = {
    "data_sources": [
        ("emr", [r"전자의무기록", r"\bEMR\b", r"의무기록"]),
        ("cdw", [r"\bCDW\b", r"임상데이터\s*웨어하우스", r"데이터\s*웨어하우스"]),
        ("pacs", [r"\bPACS\b", r"DICOM"]),
        ("lis", [r"검사\s*결과", r"\bLIS\b", r"혈액\s*검사", r"혈청"]),
        ("biobank", [r"인체유래물", r"바이오\s*뱅크"]),
        ("registry", [r"레지스트리"]),
        ("nhis", [r"건강보험공단", r"건강보험심사평가원", r"심평원", r"공단\s*자료"]),
        ("public", [r"공개\s*데이터", r"공공\s*데이터"]),
        ("survey", [r"설문", r"면담"]),
    ],
    "sensitive_categories": [
        ("genetic", [r"유전체", r"유전자", r"유전\s*정보", r"\bDNA\b", r"엑솜", r"시퀀싱"]),
        ("mental_health", [r"정신\s*질환", r"우울증", r"조현병", r"정신\s*건강", r"정신건강의학과"]),
        ("hiv_std", [r"\bHIV\b", r"에이즈", r"\bAIDS\b", r"성매개", r"매독"]),
        ("substance", [r"알코올\s*의존", r"약물\s*중독", r"마약"]),
        ("sexual_reproductive", [r"인공\s*임신\s*중절", r"임신\s*중절", r"성생활"]),
        ("criminal", [r"범죄\s*경력"]),
        ("biometric", [r"지문", r"홍채", r"생체\s*인식"]),
    ],
    "vulnerable_populations": [
        ("children", [r"소아", r"아동", r"미성년", r"청소년", r"신생아", r"영아", r"만\s*1[0-9]\s*세\s*미만"]),
        ("pregnant", [r"임산부", r"임신부", r"산모", r"태아"]),
        ("cognitively_impaired", [r"치매", r"인지\s*장애", r"의사결정\s*능력"]),
        ("prisoners", [r"수용자", r"수감"]),
        ("terminally_ill", [r"말기\s*환자", r"호스피스"]),
    ],
    "unstructured_data": [
        ("free_text", [r"경과\s*기록", r"판독문", r"자유\s*기술", r"소견서"]),
        ("medical_images", [r"\bCT\b", r"\bMRI\b", r"X-?ray", r"DICOM", r"초음파", r"의료\s*영상"]),
        ("biosignals", [r"심전도", r"\bECG\b", r"\bEEG\b", r"뇌파"]),
        ("audio_video", [r"음성\s*녹음", r"동영상", r"녹화"]),
    ],
}
IDENTIFIER_RULES: list[tuple[str, list[str]]] = [
    ("rrn", [r"주민\s*등록\s*번호", r"주민번호"]),
    ("name", [r"성명", r"이름", r"환자명"]),
    ("mrn", [r"등록번호", r"환자번호", r"병록번호", r"차트번호"]),
    ("phone", [r"전화번호", r"연락처", r"휴대폰"]),
    ("email", [r"이메일", r"e-?mail"]),
    ("address", [r"주소", r"거주지"]),
    ("birth_date", [r"생년월일"]),
    ("photo", [r"얼굴\s*사진", r"안면\s*사진"]),
]
FLAG_RULES: dict[str, list[str]] = {
    "external_transfer": [r"외부\s*기관에\s*제공", r"제3자\s*제공", r"외부로\s*반출", r"외부\s*반출", r"공동연구기관에\s*제공"],
    "international_transfer": [r"국외\s*이전", r"해외\s*(?:기관|이전|반출)"],
    "data_linkage": [r"결합전문기관", r"가명정보\s*결합", r"데이터\s*결합", r"자료\s*결합"],
    "ai_ml_development": [r"인공지능", r"딥\s*러닝", r"머신\s*러닝", r"기계\s*학습", r"\bAI\s*모델", r"신경망"],
    "multicenter": [r"다기관", r"참여\s*기관", r"공동연구기관"],
    "rare_disease": [r"희귀\s*질환"],
    "direct_contact": [r"설문", r"면담", r"인터뷰", r"추가\s*채혈"],
}
STUDY_TYPE_RULES: list[tuple[str, list[str], list[str]]] = [
    # (연구 유형, 필수 패턴(모두), 보조 패턴(하나 이상))
    ("imaging_ai", [], [r"(?:CT|MRI|X-?ray|영상|DICOM|PACS).{0,40}(?:인공지능|딥\s*러닝|머신\s*러닝|신경망|AI)",
                        r"(?:인공지능|딥\s*러닝|머신\s*러닝|신경망|AI).{0,40}(?:CT|MRI|X-?ray|영상|DICOM)"]),
    ("data_linkage", [], [r"결합전문기관", r"가명정보\s*결합"]),
    ("prospective_observational", [], [r"전향적"]),
    ("survey", [], [r"설문\s*(?:조사|연구)", r"면담\s*연구", r"인터뷰\s*연구"]),
    ("cdw_analysis", [], [r"\bCDW\b", r"임상데이터\s*웨어하우스"]),
    ("registry", [], [r"레지스트리", r"코호트\s*자료"]),
    ("retrospective_chart_review", [], [r"후향적", r"의무기록\s*(?:조사|열람|검토)", r"차트\s*리뷰"]),
]
NEGATION = re.compile(r"않|미수집|제외|삭제|대체|일련번호|가명|암호화|비식별|없이|불포함|배제|금지|하지\s*않")


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?。])\s+|\n", text)
    return [p.strip() for p in parts if p.strip()]


def _find(patterns: list[str], text: str, *, negation: bool = False) -> str | None:
    """패턴이 (부정되지 않은 문장에서) 나타나면 그 문장을 반환."""
    for sentence in _sentences(text):
        for pat in patterns:
            if re.search(pat, sentence, re.I):
                if negation and NEGATION.search(sentence):
                    continue
                return sentence
    return None


def _entry(value: Any, conf: float, snippet: str, method: str) -> dict[str, Any]:
    return {"value": value, "confidence": round(conf, 2), "source_snippet": (snippet or "")[:200], "method": method}


# ====================================================================== 규칙 기반 추출
def extract_fields(text: str) -> dict[str, dict[str, Any]]:
    parser = SectionParser()
    found, lines = parser.sections(text)
    out: dict[str, dict[str, Any]] = {}

    def best_text(key: str) -> dict[str, Any] | None:
        cands = found.get(key) or []
        if not cands:
            return None
        return max(cands, key=lambda c: (c["conf"], min(len(c["value"]), 4000)))

    for key, target in (("title", "title"), ("objectives", "objectives"), ("background", "background"),
                        ("methods", "methods"), ("target_population", "target_population"),
                        ("inclusion_criteria", "inclusion_criteria"), ("exclusion_criteria", "exclusion_criteria"),
                        ("sample_size_justification", "sample_size_justification"), ("privacy", "access_controls"),
                        ("pi_name", "pi_name"), ("department", "department")):
        c = best_text(key)
        if c:
            value = c["value"][:4000]
            if key == "title":
                value = value.splitlines()[0].strip()[:300]
            out[target] = _entry(value, c["conf"], c["snippet"], c["method"])

    if "title" not in out:  # 제목 칸이 없으면 첫 의미 있는 줄
        for ln in lines[:15]:
            t = ln.lstrip("#").strip()
            if 10 <= len(t) <= 150 and not re.search(r"계획서|신청서|서식|양식|protocol", t, re.I) and " | " not in t:
                out["title"] = _entry(NUMBERING.sub("", t).strip(), 0.4, t, "first_line")
                break

    # 대상자 수
    for c in sorted(found.get("expected_subjects", []), key=lambda c: -c["conf"]):
        n = parse_count(c["value"])
        if n:
            out["expected_subjects"] = _entry(n, c["conf"], c["snippet"], c["method"])
            break
    if "expected_subjects" not in out:
        m = re.search(r"(?:대상자|증례|표본)\s*(?:수)?\s*(?:는|은|:|：)?\s*(?:약|총)?\s*(\d{1,3}(?:,\d{3})+|\d+)\s*(?:명|례)", text)
        if m:
            out["expected_subjects"] = _entry(int(m.group(1).replace(",", "")), 0.55, m.group(0), "keyword")

    # 기간
    for key, (start_f, end_f) in (("study_period", ("study_start", "study_end")),
                                  ("data_period", ("data_period_start", "data_period_end"))):
        for c in sorted(found.get(key, []), key=lambda c: -c["conf"]):
            start, end = parse_period(c["value"])
            if start or end:
                if start:
                    out[start_f] = _entry(start.isoformat(), c["conf"], c["snippet"], c["method"])
                if end:
                    out[end_f] = _entry(end.isoformat(), c["conf"], c["snippet"], c["method"])
                break

    # 수집 항목
    c = best_text("data_items")
    if c:
        items = _split_items(c["value"])
        if items:
            out["data_items"] = _entry("\n".join(items), c["conf"], c["snippet"], c["method"])

    # 동의
    consent = best_text("consent")
    consent_text = (consent.get("heading", "") + "\n" + consent["value"]) if consent else text
    conf_base = consent["conf"] if consent else 0.5
    if WAIVER_RE.search(consent_text):
        snippet = _find([WAIVER_RE.pattern], consent_text) or ""
        out["consent_plan"] = _entry("waiver", conf_base if consent else 0.5, snippet, "section" if consent else "keyword")
        if consent:
            out["waiver_impracticable_detail"] = _entry(consent["value"][:1000], conf_base, consent["snippet"], "section")
        reasons = [k for k, words in WAIVER_REASON_KEYWORDS.items() if any(w in consent_text for w in words)]
        if reasons:
            out["waiver_impracticable_reasons"] = _entry(reasons, 0.6, snippet, "keyword")
    elif OBTAIN_RE.search(consent_text):
        snippet = _find([OBTAIN_RE.pattern], consent_text) or ""
        out["consent_plan"] = _entry("obtain", conf_base if consent else 0.5, snippet, "section" if consent else "keyword")

    # 보관·폐기
    retention = best_text("retention")
    if retention:
        years = parse_years(retention["value"])
        if years:
            out["retention_years"] = _entry(years, retention["conf"], retention["snippet"], "section")
        out["destruction_method"] = _entry(retention["value"][:500], retention["conf"], retention["snippet"], "section")

    # 연구비
    funding = best_text("funding")
    if funding:
        value = funding["value"].splitlines()[0].strip()
        if re.fullmatch(r"(해당\s*)?없음|무|-|해당사항\s*없음|N/?A", value, re.I):
            value = ""
        out["funding_source"] = _entry(value[:200], funding["conf"], funding["snippet"], funding["method"])

    _keyword_inference(text, found, out)
    return out


def _split_items(value: str) -> list[str]:
    items: list[str] = []
    for ln in value.splitlines():
        ln = NUMBERING.sub("", ln).strip()
        if not ln:
            continue
        if " | " in ln:
            items.extend(c.strip() for c in ln.split("|") if c.strip())
        elif re.search(r"[,、]", ln) and len(ln) < 400:
            items.extend(p.strip(" .") for p in re.split(r"[,、]", ln) if p.strip(" ."))
        else:
            items.append(ln)
    return list(dict.fromkeys(i for i in items if i))[:80]


def _keyword_inference(text: str, found: dict[str, list[dict[str, Any]]], out: dict[str, dict[str, Any]]) -> None:
    for key, rules in KEYWORD_RULES.items():
        values, snippet = [], ""
        for value, pats in rules:
            hit = _find(pats, text, negation=(value == "survey"))
            if hit:
                values.append(value)
                snippet = snippet or hit
        if values:
            out[key] = _entry(values, 0.55, snippet, "keyword")

    # 식별자: 수집 항목·개인정보 섹션 우선(부정 문맥 제외)
    scope = "\n".join(c["value"] for k in ("data_items", "privacy") for c in found.get(k, [])) or text
    ids, snippet = [], ""
    for value, pats in IDENTIFIER_RULES:
        hit = _find(pats, scope, negation=True)
        if hit:
            ids.append(value)
            snippet = snippet or hit
    if ids:
        out["identifiers_collected"] = _entry(ids, 0.5, snippet, "keyword")

    for flag, pats in FLAG_RULES.items():
        hit = _find(pats, text, negation=True)
        if hit:
            out[flag] = _entry(True, 0.55, hit, "keyword")

    if (hit := _find([r"후향적", r"기존\s*자료", r"과거\s*(?:자료|영상|기록)"], text)) and "direct_contact" not in out:
        out["existing_data_only"] = _entry(True, 0.6, hit, "keyword")
    elif hit := _find([r"전향적"], text):
        out["existing_data_only"] = _entry(False, 0.6, hit, "keyword")

    for value, required, any_of in STUDY_TYPE_RULES:
        if all(re.search(p, text, re.I) for p in required):
            hit = _find(any_of, text, negation=(value == "survey"))
            if hit:
                out["study_type"] = _entry(value, 0.55, hit, "keyword")
                break

    privacy_text = "\n".join(c["value"] for c in found.get("privacy", [])) or text
    if hit := _find([r"가명"], privacy_text):
        out["deid_level"] = _entry("pseudonymized", 0.6, hit, "keyword")
    elif hit := _find([r"익명"], privacy_text):
        out["deid_level"] = _entry("anonymized", 0.6, hit, "keyword")


# ====================================================================== 기관 고유 항목(Fork A 사용자 정의 필드)
def extract_custom_fields(text: str, field_defs: list[Any]) -> dict[str, dict[str, Any]]:
    """기관이 정의한 추가 항목(라벨)을 문서 제목·표에서 찾는다. 결과 키는 'custom:<key>'."""
    out: dict[str, dict[str, Any]] = {}
    defs = []
    for d in field_defs or []:
        get = d.get if isinstance(d, dict) else (lambda k, _d=d: getattr(_d, k, None))
        key = get("key") or get("name") or get("field")
        label = get("label") or get("title") or key
        if key and label:
            defs.append((str(key), str(label)))
    if not defs:
        return out
    labels = {norm(label): (key, label) for key, label in defs}
    for raw in text.splitlines():
        line = raw.strip().lstrip("#").strip()
        candidates: list[tuple[str, str]] = []
        if " | " in line:
            cells = [c.strip() for c in line.split("|")]
            candidates = [(cells[i], cells[i + 1]) for i in range(len(cells) - 1)]
        elif m := re.match(r"^([^:：]{1,30})\s*[:：]\s*(.+)$", NUMBERING.sub("", line)):
            candidates = [(m.group(1), m.group(2))]
        for label_text, value in candidates:
            hit = labels.get(norm(NUMBERING.sub("", label_text)))
            if hit and value and f"custom:{hit[0]}" not in out:
                out[f"custom:{hit[0]}"] = {**_entry(value.strip()[:1000], 0.8, line, "custom"), "label": hit[1]}
    return out


def custom_field_defs(db: Any) -> list[Any]:
    """Fork A의 forms.schema.active_protocol_fields(db) - 없으면 빈 목록."""
    try:
        mod = importlib.import_module(f"{__package__.rsplit('.', 1)[0]}.forms.schema")
        return list(mod.active_protocol_fields(db) or [])
    except Exception:  # 모듈 없음 또는 스키마 변경
        return []


# ====================================================================== AI 보조 추출
AI_EXTRACT_SYSTEM = (
    "당신은 병원 IRB 사무국의 문서 정리 보조자입니다. 다른 병원 서식으로 작성된 연구계획서에서 지정된 항목을 찾아 "
    "우리 기관 입력 양식에 맞게 옮깁니다. 문서에 명시된 내용만 옮기고 추측하지 마십시오. 찾지 못한 항목은 빈 문자열, "
    "숫자는 0, 목록은 빈 배열, 참/거짓은 false로 두십시오. 날짜는 YYYY-MM-DD 형식으로 쓰고, 월까지만 있으면 시작일은 1일, "
    "종료일은 말일로 쓰십시오. evidence에는 각 항목을 뒷받침하는 원문 문장을 그대로(수정 없이) 인용하십시오."
)
AI_TEXT_FIELDS = ("title", "objectives", "background", "methods", "target_population", "inclusion_criteria",
                  "exclusion_criteria", "sample_size_justification", "access_controls", "waiver_impracticable_detail",
                  "destruction_method", "funding_source")
AI_DATE_FIELDS = ("study_start", "study_end", "data_period_start", "data_period_end")
AI_BOOL_FIELDS = ("existing_data_only", "direct_contact", "external_transfer", "international_transfer", "data_linkage",
                  "ai_ml_development", "multicenter", "rare_disease")
AI_LIST_FIELDS = {"data_sources": "data_sources", "identifiers_collected": "identifiers",
                  "sensitive_categories": "sensitive_categories", "vulnerable_populations": "vulnerable_populations",
                  "unstructured_data": "unstructured_data", "waiver_impracticable_reasons": "waiver_reasons"}


def ai_schema() -> dict[str, Any]:
    rs = get_ruleset()
    props: dict[str, Any] = {k: {"type": "string"} for k in AI_TEXT_FIELDS + AI_DATE_FIELDS}
    props["data_items"] = {"type": "array", "items": {"type": "string"}}
    props["expected_subjects"] = {"type": "integer"}
    props["retention_years"] = {"type": "integer"}
    props["study_type"] = {"type": "string", "enum": [*rs.catalog("study_types"), ""]}
    props["deid_level"] = {"type": "string", "enum": ["anonymized", "pseudonymized", "identifiable", ""]}
    props["consent_plan"] = {"type": "string", "enum": ["obtain", "waiver", ""]}
    for key, catalog in AI_LIST_FIELDS.items():
        props[key] = {"type": "array", "items": {"type": "string", "enum": list(rs.catalog(catalog))}}
    for key in AI_BOOL_FIELDS:
        props[key] = {"type": "boolean"}
    props["evidence"] = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"field": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["field", "quote"],
            "additionalProperties": False,
        },
    }
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _squash(s: str) -> str:
    return re.sub(r"\s+", "", s)


def ai_extract_fields(text: str, *, source_text: str | None = None) -> dict[str, dict[str, Any]]:
    """Claude 구조화 출력으로 추출. 인용(evidence)이 원문에 있으면 신뢰도 0.8, 없으면 0.5."""
    from ..ai import client

    data = client.structured_call(
        system=AI_EXTRACT_SYSTEM,
        content=[client.text_document_block(text[:150000], "연구계획서"),
                 {"type": "text", "text": "위 연구계획서에서 지정된 항목을 추출하십시오."}],
        schema=ai_schema(), effort="high", max_tokens=16000, purpose="계획서 정보 추출",
    )
    corpus = _squash(source_text or text)
    quotes: dict[str, str] = {}
    for ev in data.get("evidence", []) or []:
        f, q = str(ev.get("field", "")), str(ev.get("quote", ""))
        if f and q and f not in quotes:
            quotes[f] = q
    out: dict[str, dict[str, Any]] = {}

    def put(key: str, value: Any) -> None:
        quote = quotes.get(key, "")
        verified = bool(quote) and _squash(quote) in corpus
        out[key] = _entry(value, 0.8 if verified else 0.5, quote, "ai" if verified else "ai_unverified")

    for key in AI_TEXT_FIELDS:
        if str(data.get(key, "")).strip():
            put(key, str(data[key]).strip())
    for key in AI_DATE_FIELDS:
        v = str(data.get(key, "")).strip()
        try:
            put(key, date.fromisoformat(v).isoformat())
        except ValueError:
            continue
    if data.get("data_items"):
        put("data_items", "\n".join(str(x) for x in data["data_items"] if str(x).strip()))
    for key in ("expected_subjects", "retention_years"):
        if isinstance(data.get(key), int) and data[key] > 0:
            put(key, int(data[key]))
    for key in ("study_type", "deid_level", "consent_plan"):
        if data.get(key):
            put(key, data[key])
    for key in AI_LIST_FIELDS:
        if data.get(key):
            put(key, list(dict.fromkeys(data[key])))
    for key in AI_BOOL_FIELDS:
        if data.get(key) is True:
            put(key, True)
    return out


def merge_fields(rule: dict[str, dict[str, Any]], ai: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """신뢰도가 높은 쪽을 택하고, 두 방법의 값이 같으면 신뢰도를 올린다."""
    merged = dict(rule)
    for key, a in ai.items():
        r = merged.get(key)
        if r is None:
            merged[key] = a
        elif r["value"] == a["value"]:
            merged[key] = {**r, "confidence": round(min(0.97, max(r["confidence"], a["confidence"]) + 0.1), 2),
                           "method": f"{r['method']}+ai"}
        elif a["confidence"] > r["confidence"]:
            merged[key] = {**a, "alternative": r["value"]}
        else:
            merged[key] = {**r, "alternative": a["value"]}
    return merged


def labeled(fields: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """화면 표시용: 라벨을 붙여 정해진 순서로."""
    order = list(FIELD_LABELS)
    rows = []
    for key, entry in sorted(fields.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in order else 999, kv[0])):
        rows.append({"key": key, "label": entry.get("label") or FIELD_LABELS.get(key, key), **entry})
    return rows
