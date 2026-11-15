"""MVP scoring of qualifying findings without a manual review step."""
from __future__ import annotations

from typing import TypedDict

from .checks import CRITERIA_PATH, _load, criteria
from .models import QCRun
from .scoring import SeverityCounts

SEVERITIES = ("critical", "major", "minor")
VERDICTS = {"PASS": "통과", "REVISE": "보완 필요", "FAIL": "미통과", "HOLD": "채점 불가"}


class DocumentScore(TypedDict):
    issues: int
    deduction: int


class AreaScore(TypedDict):
    label: str
    points: int
    score: int
    deduction: int
    counts: SeverityCounts
    issue_ids: list[int]


def score_run(run: QCRun) -> None:
    """Store the score and the criteria used so subsequent displays agree."""
    configured = criteria()
    defaults = _load(str(CRITERIA_PATH), CRITERIA_PATH.stat().st_mtime)
    scoring = {**defaults["scoring"], **configured.get("scoring", {})}
    weights = {severity: int(scoring[severity]) for severity in SEVERITIES}
    areas: dict[str, AreaScore] = {
        key: AreaScore(label=spec["label"], points=spec["points"], score=spec["points"], deduction=0,
                       counts=SeverityCounts(critical=0, major=0, minor=0), issue_ids=[])
        for key, spec in scoring["areas"].items()
    }
    area_by_category = {category: key for key, spec in scoring["areas"].items() for category in spec["categories"]}
    documents = {doc["key"]: DocumentScore(issues=0, deduction=0) for doc in run.docs}
    readable_keys = {doc["key"] for doc in run.docs if doc["chars"]}
    eligible = (
        issue for issue in run.issues
        if readable_keys and issue.get("doc") in (readable_keys | {None, ""})
        and issue["category"] in area_by_category and issue["severity"] in weights
        and issue.get("review", "candidate") in ("candidate", "confirmed")
        and not (issue.get("merged_into") or issue.get("advice") or issue.get("confidence") == "ai_unverified"
                 or issue.get("result") in ("needs_check", "na", "match") or issue.get("status") == "unchecked")
    )
    # 감점하지 않지만 제출 전에 사람이 확인해야 하는 항목(적용 여부 확인, 인용 미검증 AI 지적, 조언).
    notes = [
        issue["id"] for issue in run.issues
        if readable_keys and issue.get("doc") in (readable_keys | {None, ""}) and not issue.get("merged_into")
        and issue.get("review", "candidate") in ("candidate", "confirmed")
        and (issue.get("advice") or issue.get("confidence") == "ai_unverified"
             or issue.get("result") == "needs_check" or issue.get("status") == "unchecked")
    ]
    seen: set[str | int] = set()
    deductions: dict[int, int] = {}
    for issue in sorted(eligible, key=lambda item: (SEVERITIES.index(item["severity"]), item["id"])):
        group = issue.get("group") or issue["id"]
        if group in seen:
            continue
        seen.add(group)
        severity = issue["severity"]
        area = areas[area_by_category[issue["category"]]]
        applied = min(area["score"], weights[severity])
        area["score"] -= applied
        area["deduction"] += applied
        area["counts"][severity] += 1
        area["issue_ids"].append(issue["id"])
        deductions[issue["id"]] = applied
        if issue.get("doc") in documents:
            doc = documents[issue["doc"]]
            doc["issues"] += 1
            doc["deduction"] += applied
    critical = sum(area["counts"]["critical"] for area in areas.values())
    run.score = sum(area["score"] for area in areas.values()) if readable_keys else 0
    run.verdict = ("HOLD" if not readable_keys else "FAIL" if critical and scoring["critical_fails"]
                   else "PASS" if run.score >= scoring["pass_score"] else "REVISE")
    run.counts = {
        "na": not readable_keys, "areas": areas, "documents": documents, "deductions": deductions, "notes": notes,
        "max_score": sum(area["points"] for area in areas.values()), "scoring": scoring,
        "required": configured.get("required", {}),
    }
