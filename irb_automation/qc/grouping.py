"""Mark parallel evidence about an identical discrepancy as one finding."""
import re

from .rules import legacy_flag


def _restates(known: dict, issue: dict) -> bool:
    """AI가 규칙이 이미 찾은 항목을 같은 문서에서 다시 지적했는지(같은 규칙 번호 + 제목의 핵심어 일치)."""
    rule_id = known.get("rule_id") or legacy_flag(known.get("code", ""), known.get("status", ""))[0]
    if not rule_id or rule_id != issue.get("rule_id") or known.get("doc") != issue.get("doc"):
        return False
    subject = known.get("title", "").rpartition(":")[2]
    words = {word for word in re.findall(r"[0-9A-Za-z가-힣]{3,}", subject)}
    return any(word in issue.get("title", "") + issue.get("evidence", "") for word in words)


def merge_ai_evidence(rule_issues: list[dict], ai_issues: list[dict]) -> list[dict]:
    """Treat verified overlapping evidence for the same flag as a single finding."""
    remaining = []
    for issue in ai_issues:
        cited = {(loc["doc"], "".join(loc["quote"].split())) for loc in issue.get("locations", []) if loc.get("quote")}
        duplicate = None
        if cited and issue.get("confidence") == "ai_verified":
            for known in rule_issues:
                legacy_rule, legacy_label = legacy_flag(known.get("code", ""), known.get("status", ""))
                known_rule = known.get("rule_id") or legacy_rule
                if known_rule and issue.get("rule_id") and known_rule != issue["rule_id"]:
                    continue
                same_flag = (known_rule == issue.get("rule_id")
                             and set(known.get("flags") or [legacy_label]) & set(issue.get("flags", [])))
                if not same_flag and known.get("category") != issue.get("category"):
                    continue
                quotes = [(loc["doc"], "".join(loc["quote"].split())) for loc in known.get("locations", []) if loc.get("quote")]
                if all(any(doc == d and (q in quote or quote in q) for d, q in quotes) for doc, quote in cited):
                    duplicate = known
                    break
            duplicate = duplicate or next((known for known in rule_issues if _restates(known, issue)), None)
        if duplicate is None:
            remaining.append(issue)
        else:
            duplicate["ai_evidence"] = [*duplicate.get("ai_evidence", []), *issue["locations"]]
    return remaining


def group_issues(issues: list[dict]) -> None:
    groups: dict[str, list[dict]] = {}
    for issue in issues:
        if issue.get("group"):
            groups.setdefault(issue["group"], []).append(issue)
    for group in groups.values():
        if len(group) < 2:
            continue
        primary = next((i for i in group if i["category"] == "cross" and i["result"] == "mismatch"), group[0])
        primary["count"] = len(group)
        for issue in group:
            if issue is primary:
                continue
            issue["merged_into"] = primary["id"]
            primary["flags"] = list(dict.fromkeys(primary.get("flags", []) + issue.get("flags", [])))
            locations = primary.setdefault("locations", [])
            locations.extend(loc for loc in issue.get("locations", []) if loc not in locations)
