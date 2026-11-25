"""Extract explicitly labeled facts used for additional QC comparisons."""
import re
from decimal import Decimal

from .facts import Fact

_LABELS = (
    ("study_no", r"(?:연구|과제)\s*번호\s*[:：]\s*([\w-]+)"),
    ("site", r"(?:수행|연구)\s*기관\s*[:：]\s*([^\n,;]+)"),
    ("age_range", r"(?:선정\s*연령|대상\s*연령|연령)\s*[:：]\s*((?:만\s*)?\d+\s*세\s*(?:이상|초과)\s*(?:[~∼,·]\s*)?\d+\s*세\s*(?:이하|미만))"),
    ("deid_level", r"(?:처리\s*수준|비식별\s*방식)\s*[:：]\s*(가명|익명)"),
)
_VOLUME = r"(?P<amount>(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(?:mL|㎖)(?![A-Za-z/])"
_BLOOD = r"(?:정맥혈|전혈|혈액|채혈)"
_BLOOD_VOLUME = re.compile(
    rf"{_BLOOD}\s*(?:(?:채취|채혈)(?:량)?\s*|량\s*)?(?:은|는|[:：(|])?\s*"
    rf"(?:완료\s*/\s*)?{_VOLUME}(?!\s*(?:씩\s*분주|단위로\s*분주))", re.I,
)
_GENERIC_VOLUME = re.compile(rf"검체\s*채취(?:량)?\s*[:：]?\s*{_VOLUME}", re.I)
_TOTAL_VOLUME = re.compile(
    rf"(?:총|전체|합계)\s*(?:예정\s*)?(?:채혈량|채취량|채혈\s*양|채취\s*양|"
    rf"[\d,]+\s*명(?:으로)?부터)\s*(?:은|는|[:：])?\s*"
    rf"(?:[\d,.]+\s*(?:mL|㎖)\s*[×xX*]\s*[\d,]+\s*명\s*=\s*)?{_VOLUME}", re.I,
)
_QUALIFIED_VOLUME = re.compile(r"최대|최소|필요\s*시|이내|미만|이하|이상|추가|잔여|분주|재채혈|(?:시험|대조|실험|치료)군|(?:첫|두\s*번째|\d+\s*차)\s*방문")
_PAYMENT = re.compile(
    r"(?P<label>교통비|사례비|손상\s*보상|참여\s*(?:비용|보상))"
    r"(?:\s*명목으로|\s*지급\s*\|\s*[^|.]{0,20}|[^.!?|\d]{0,20})"
    r".{0,35}?(?<![\d.,])(?P<amount>\d{1,3}(?:,\d{3})+|\d+)\s*(?P<unit>만\s*)?원"
)
_PARTICIPATION = re.compile(r"방문|1인당|참여\s*(?:보상|사례비)")
_QUALIFIED_PAYMENT = re.compile(r"최대|최소|회당|1회당|실비|이내|(?:시험|대조|실험|치료)군")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?。])\s+(?=[^\d])")


def _collection_and_payment(text: str) -> list[Fact]:
    facts: list[Fact] = []
    blood_document = bool(_BLOOD_VOLUME.search(text))
    for line in text.splitlines():
        for sentence in _SENTENCE_BREAK.split(line):
            totals = list(_TOTAL_VOLUME.finditer(sentence)) if blood_document else []
            claims = [
                (hit, scope) for pattern, scope in ((_BLOOD_VOLUME, "채혈"), (_GENERIC_VOLUME, "검체채취"))
                for hit in pattern.finditer(sentence)
                if not any(total.start() <= hit.start() < total.end() for total in totals)
                and not re.search(r"(?:총|전체|합계)\s*$", sentence[:hit.start()])
            ]
            claims.extend((hit, "전체 채혈") for hit in totals)
            for hit, scope in claims:
                raw = hit[0].strip()
                amount = Decimal(hit["amount"].replace(",", "")).normalize()
                evidence = sentence.strip()
                if len(evidence) > 200:
                    evidence = raw
                facts.append(Fact("sample_volume", f"{amount:f}mL", raw, evidence, scope=scope,
                                  ambiguous=bool(_QUALIFIED_VOLUME.search(sentence))))
            for hit in _PAYMENT.finditer(sentence):
                label = re.sub(r"\s+", "", hit["label"])
                if label in ("교통비", "사례비", "참여보상") and _PARTICIPATION.search(sentence):
                    label = "참여 지급액"
                amount = int(hit["amount"].replace(",", "")) * (10_000 if hit["unit"] else 1)
                raw = hit[0].strip()
                quote = sentence.strip()
                if len(quote) > 200:
                    quote = raw
                facts.append(Fact("expense", str(amount), raw, quote, scope=label,
                                  ambiguous=bool(_QUALIFIED_PAYMENT.search(sentence))))
    return facts


def extract_extra(text: str) -> list[Fact]:
    facts = []
    for line in text.splitlines():
        for key, pattern in _LABELS:
            for hit in re.finditer(pattern, line):
                raw = hit[1].strip()
                facts.append(Fact(key, re.sub(r"\s+", "", raw).lower(), raw, line))
        role = re.match(r"\s*(연구\s*문의|권리\s*문의|IRB\s*연락처)\s*[:：]", line)
        if role:
            scope = "연구 문의" if role[1].startswith("연구") else "권리 문의"
            for hit in re.finditer(r"\b\d{2,4}\s*[-) ]\s*\d{3,4}\s*[- ]\s*\d{4}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", line):
                raw = hit[0]
                kind = "email" if "@" in raw else "phone"
                facts.append(Fact("contact", raw.lower() if "@" in raw else re.sub(r"\D", "", raw),
                                  raw, line, field=f"contact_{kind}", scope=scope))
        conditional = bool(re.search(r"필요\s*시|증량|감량|교대", line))
        arms = sorted(set(re.findall(r"시험군|실험군|치료군|대조군", line)))
        scope = arms[0] if len(arms) == 1 else ""
        for hit in re.finditer(r"(?:1일|하루|매일)\s*(\d+)\s*회", line):
            facts.append(Fact("dose_freq", hit[1], hit[0], line, scope=scope, ambiguous=conditional or len(arms) > 1))
        if re.search(r"투여|투약|1회\s*\d+\s*(?:mg|㎎|g)", line):
            for hit in re.finditer(r"경구|정맥|피하|근육|(?<![A-Za-z])(?:PO|IV|SC|IM)(?![A-Za-z])", line, re.I):
                normalized = {"po": "경구", "iv": "정맥", "sc": "피하", "im": "근육"}.get(hit[0].lower(), hit[0])
                facts.append(Fact("dose_route", normalized, hit[0], line, scope=scope, ambiguous=conditional or len(arms) > 1))
    return facts + _collection_and_payment(text)
