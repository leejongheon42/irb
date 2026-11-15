"""Compare source cover identity without treating extracted text as verification."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ..docproc.text import ExtractedText


@dataclass(frozen=True, slots=True)
class CoverIdentity:
    series: str
    number: str
    version: str


def check_cover(identity: CoverIdentity, extracted: ExtractedText) -> Literal["matched", "mismatch", "manual_required"]:
    """Only a complete, unambiguous cover match passes the automatic comparison."""
    if extracted.warnings or ":preview" in extracted.method or ":error" in extracted.method:
        return "manual_required"
    cover = extracted.text[:2500]
    numbered = re.search(
        r"(?P<series>공용위원회\s*별지|시행규칙\s*별지|권고(?:서식)?|별지)"
        r"[\s\[\]()]*제?\s*(?P<number>\d+(?:\s*-\s*\d+)?)\s*호",
        cover,
    )
    version = re.search(r"(?:ver(?:sion)?\.?|판본)\s*([0-9]+(?:\.[0-9]+)+)", cover, re.IGNORECASE)
    if numbered is None or version is None:
        return "manual_required"
    series = re.sub(r"\s", "", numbered.group("series"))
    series = {"권고": "권고서식", "공용위원회별지": "공용위원회 별지", "시행규칙별지": "시행규칙 별지"}.get(series, series)
    number = re.sub(r"\s", "", numbered.group("number"))
    if (series, number, version.group(1)) != (identity.series, identity.number, identity.version):
        return "mismatch"
    return "matched"
