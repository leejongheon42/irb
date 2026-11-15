"""Add the recorded value to the corresponding document comparison."""
from typing import TYPE_CHECKING

from .facts import record_facts, restyle
from .rules import cross_metadata

if TYPE_CHECKING:
    from ..models import Protocol
    from .checks import QCDoc


def record_findings(docs: list["QCDoc"], protocol: "Protocol | None", cross: list[dict]) -> list[dict]:
    from .checks import _issue, criteria

    if protocol is None or not criteria().get("record_reference", True):
        return []
    out = []
    for ref in record_facts(protocol):
        if "?" in ref.value or ref.key not in criteria().get("cross_fields", {}):
            continue
        key = ref.key
        field = "subjects_total" if key == "subjects" else key
        entries = [(d, f) for d in docs for f in d.facts
                   if f.key == key and (f.field or f.key) == field and not f.scope and not f.ambiguous]
        if not any(f.value != ref.value for _, f in entries):
            continue
        locations = [{"doc": d.key, "name": d.name, "quote": f.line, "value": f.raw, "norm": f.value,
                      "unique": bool(f.raw and d.text.count(f.raw) == 1)} for d, f in entries]
        record = {"doc": "record", "name": "과제 입력값", "quote": "", "value": ref.raw, "norm": ref.value,
                  "unique": False}
        existing = next((i for i in cross if i.get("fact_key") == key and i.get("field") == field
                         and not i.get("scope")), None)
        if existing:
            existing["locations"].append(record)
            existing["record_value"] = ref.value
            continue
        _, _, severity = cross_metadata(key)
        label = criteria()["cross_fields"][key]["label"]
        fixes = []
        for loc in locations:
            if loc["norm"] != ref.value and loc["unique"]:
                new = restyle(key, loc["value"], ref)
                if new and new != loc["value"]:
                    fix = {"doc": loc["doc"], "old": loc["value"], "new": new}
                    if fix not in fixes:
                        fixes.append(fix)
        out.append({**_issue(severity, "record", f"{label}이(가) 과제 정보와 다릅니다", code=f"RECORD_{key.upper()}",
                             detail=f"과제 정보: {ref.raw}", fixes=fixes),
                    "field": field, "fact_key": key, "locations": [*locations, record], "record_value": ref.value})
    return out
