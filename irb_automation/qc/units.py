"""Attach registry metadata and preserve observed check units."""
from .checks import QCDoc, criteria
from .compare import comparisons
from .rules import cross_metadata, flag_meta, legacy_flag, rules


def annotate(issue: dict) -> dict:
    code = issue.get("code", "")
    rule, flag = legacy_flag(code, issue.get("status", ""))
    rule = issue.get("rule_id") or rule
    flags = issue.get("flags") or ([flag] if flag else [])
    issue.setdefault("confidence", "rule" if issue.get("source") != "ai" else "ai_unverified")
    issue.setdefault("review", "candidate")
    issue.setdefault("result", "missing" if issue.get("status") in ("missing", "partial") else "mismatch")
    if "권리 설명 모순" in flags:
        issue["result"] = "mismatch"
    if rule and flags:
        meta = flag_meta(rule, flags[0])
        issue.update(rule_id=rule, flags=flags, rule_version=rules()["version"],
                     rule_source=meta.get("source", "velivo"),
                     proposed_severity=meta.get("severity") or issue["severity"])
    if code in ("NO_TEXT", "VERSION_MISSING", "RETENTION_SHORT", "CONSENT_PLAN_MISMATCH"):
        issue.update(result="needs_check", reason={"NO_TEXT": "unreadable",
                     "CONSENT_PLAN_MISMATCH": "scope_unclear"}.get(code, "basis_unverified"))
    if code.startswith("REQ_") and not issue.get("basis") and not code.endswith("_VOLUNTARY"):
        issue["basis_pending"] = True
        if criteria().get("required_unverified", "needs_check") == "needs_check":
            issue["original_result"] = issue["result"]
            issue.update(result="needs_check", reason="basis_unverified")
    if issue.get("status") == "unchecked" or issue["confidence"] == "ai_unverified":
        issue.update(result="needs_check", reason=issue.get("reason") or "review_pending")
    if issue["result"] == "needs_check":
        issue["fixes"] = []
        issue["question"] = issue.get("question") or "원문과 이 항목의 적용 근거를 확인해 주세요."
    if issue.get("source") == "ai":
        issue["fixes"] = []
    return issue


def observed_units(docs: list[QCDoc], issues: list[dict], seeded: list[dict]) -> list[dict]:
    units = list(seeded)
    linked = set()
    for unit in units:
        if unit["result"] == "needs_check":
            match = next((i for i in issues if i.get("rule_id") == unit["rule"] and i.get("field") == unit["field"]), None)
            if match:
                unit["issue"] = match["id"]
                linked.add(match["id"])
    for issue in issues:
        if issue["id"] in linked or issue.get("merged_into") or issue.get("advice"):
            continue
        locs = issue.get("locations", [])
        keys = list(dict.fromkeys(loc["doc"] for loc in locs))
        units.append({"rule": issue.get("rule_id") or issue.get("category", ""), "label": issue.get("label", ""),
                      "field": issue.get("field") or issue.get("code"), "scope": issue.get("scope") or "",
                      "docs": keys or issue.get("docs") or ([issue["doc"]] if issue.get("doc") else []),
                      "locations": locs, "result": issue["result"], "reason": issue.get("reason", ""),
                      "issue": issue["id"]})
    if any(not d.text.strip() for d in docs):
        return units
    fields = criteria().get("cross_fields", {})
    for row in comparisons(docs):
        locs = row["locations"]
        if row["key"] not in fields or row["uncertain"] or row["different"]:
            continue
        if any(i["category"] == "record" and i.get("fact_key") == row["key"] and i.get("field") == row["field"]
               and not row["scope"] for i in issues):
            continue
        rule, _, _ = cross_metadata(row["key"])
        if any(u["rule"] == rule and u["result"] == "na" for u in seeded):
            continue
        units.append({"rule": rule, "field": row["field"], "label": fields[row["key"]]["label"], "scope": row["scope"], "result": "match",
                      "docs": list(dict.fromkeys(loc["doc"] for loc in locs)), "locations": locs, "reason": ""})
    return units
