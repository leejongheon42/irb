"""Check totals whose components are explicit and comparable."""
import re
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .checks import QCDoc

_ARM_TOTAL = re.compile(
    r"(?:시험군|실험군|치료군)\s*([\d,]+)\s*명\s*[,，·및]+\s*대조군\s*([\d,]+)\s*명"
    r"\s*[,，·;]\s*(?:총|합계|전체)\s*([\d,]+)\s*명"
)
_DOSE_TOTAL = re.compile(
    r"1일\s*총\s*(\d+(?:\.\d+)?)\s*(mg|㎎|g|mL|ml|㎖)(?![\w/])"
)


def check_totals(doc: "QCDoc") -> list[dict]:
    from .checks import _issue

    out = []
    for line in doc.text.splitlines():
        hit = _ARM_TOTAL.search(line)
        if hit:
            left, right, total = (int(v.replace(",", "")) for v in hit.groups())
            if left + right != total:
                out.append({**_issue("major", "numbers", "군별 인원 합계가 총인원과 다릅니다.", doc=doc,
                                     evidence=line, phase=3, code="SUBJECTS_SUM"),
                            "field": "subjects_total", "within": "doc",
                            "group": "|".join(("E02", "subjects_total", "", *sorted((str(left + right), str(total)))))})
        if re.search(r"필요\s*시|증량|감량|교대|시험군|대조군|mg\s*/", line):
            continue
        totals = list(_DOSE_TOTAL.finditer(line))
        doses = {f.value.split("/", 1)[1] for f in doc.facts
                 if f.field == "dose_unit" and f.line == line.strip()[:200] and not f.ambiguous}
        freq = {int(f.value) for f in doc.facts if f.key == "dose_freq" and f.line == line and not f.ambiguous}
        if len(totals) != 1 or len(doses) != 1 or len(freq) != 1:
            continue
        expected, unit = totals[0].groups()
        unit = {"㎎": "mg", "ml": "mL", "㎖": "mL"}.get(unit, unit)
        daily = Decimal(expected)
        if unit == "g":
            unit, daily = "mg", daily * 1000
        dose = re.fullmatch(r"([\d.]+)(mg|mL)", next(iter(doses)))
        if dose and dose[2] == unit and Decimal(dose[1]) * next(iter(freq)) != daily:
            out.append({**_issue("major", "numbers", "1회 용량과 빈도로 계산한 하루 용량이 표기와 다릅니다.",
                                 doc=doc, evidence=line, phase=3, code="DOSE_SUM"),
                        "field": "dose_daily", "within": "doc"})
    return out
