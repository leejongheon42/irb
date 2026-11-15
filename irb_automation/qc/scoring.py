"""Score confirmed QC findings and summarize pending review."""
from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict

from .checks import criteria


class SeverityIssue(TypedDict):
    severity: str


class ScoredIssue(SeverityIssue, total=False):
    confidence: str
    status: str
    result: str
    review: str
    group: str
    merged_into: int
    advice: bool
    check_answer: dict[str, str | int]


class SeverityCounts(TypedDict):
    critical: int
    major: int
    minor: int


class Assessment(TypedDict):
    score: int | None
    projected: int
    provisional: bool
    counts: SeverityCounts
    pending: dict[str, int]
    coverage: float | None
    status: str


def assess(issues: Sequence[ScoredIssue], results: Sequence[str] = (), *, scorable: bool = True) -> Assessment:
    """Score unique, confirmed findings and report the remaining review work."""
    sc = criteria().get("scoring", {})
    weights = {s: int(sc.get(s, w)) for s, w in (("critical", 15), ("major", 7), ("minor", 2))}
    confirmed: dict[str, str] = {}
    candidate: dict[str, str] = {}
    pending = {"candidate": 0, "needs_check": 0}
    pending_keys: set[str] = set()
    for n, issue in enumerate(issues):
        key = issue.get("group") or f"#{n}"
        if issue.get("merged_into") or issue.get("advice") or issue.get("review") in ("dismissed", "accepted", "resolved"):
            continue
        if (issue.get("result") == "needs_check" or issue.get("status") == "unchecked"
                or (issue.get("confidence") == "ai_unverified" and not issue.get("check_answer"))):
            pending_keys.add(key)
            continue
        bucket = confirmed if issue.get("review") == "confirmed" else candidate
        severity = issue["severity"]
        if weights.get(severity, 0) > weights.get(bucket.get(key, ""), 0):
            bucket[key] = severity
    counts = SeverityCounts(critical=0, major=0, minor=0)
    counts.update({k: sum(s == k for s in confirmed.values()) for k in weights})
    pending["candidate"] = len(candidate.keys() - confirmed.keys())
    pending["needs_check"] = max(results.count("needs_check"), len(pending_keys))
    total = max(0, 100 - sum(weights[s] for s in confirmed.values()))
    projected = max(0, total - sum(weights[s] for k, s in candidate.items() if k not in confirmed))
    provisional = any(pending.values())
    status = ("FAIL" if counts["critical"] and sc.get("critical_fails", True) else "HOLD" if provisional or not scorable
              else "REVISE" if counts["major"] or counts["critical"] else "MINOR" if counts["minor"] else "PASS")
    applicable = [r for r in results if r != "na"]
    coverage = 1 - sum(r in ("needs_check", "candidate") for r in applicable) / len(applicable) if applicable else None
    return Assessment(score=total if scorable else None, projected=projected, provisional=provisional, counts=counts,
                      pending=pending, coverage=coverage, status=status)


