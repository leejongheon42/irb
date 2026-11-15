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


def extract_extra(text: str) -> list[Fact]:
    facts = []
    for line in text.splitlines():
        for key, pattern in _LABELS:
            for hit in re.finditer(pattern, line):
                raw = hit[1].strip()
                facts.append(Fact(key, re.sub(r"\s+", "", raw).lower(), raw, line))
        for hit in re.finditer(r"(채혈|검체\s*채취)(?:량)?\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(mL|ml|㎖)(?!\w)", line):
            facts.append(Fact("sample_volume", f"{Decimal(hit[2]):g}mL", hit[0], line, scope=hit[1].replace(" ", "")))
        for hit in re.finditer(r"(교통비|사례비|손상\s*보상|참여\s*비용)\s*[:：]?\s*([\d,]+)\s*(만\s*)?원", line):
            amount = int(hit[2].replace(",", "")) * (10_000 if hit[3] else 1)
            facts.append(Fact("expense", str(amount), hit[0], line, scope=hit[1].replace(" ", "")))
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
    return facts
