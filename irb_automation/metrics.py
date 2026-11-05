"""운영 성과지표(KPI)와 연간 운영실적 보고서.

소요시간은 IRB 측 시간(접수~결정/보완요청)과 연구자 측 시간(보완요청~재제출)을 분리해 계산한다.
(대기시간의 상당 부분이 연구자 답변 대기이므로 둘을 구분해야 IRB 처리 성과를 올바르게 볼 수 있다.)
"""
from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    Decision,
    DecisionType,
    Meeting,
    PreReview,
    Protocol,
    ReviewAssignment,
    Submission,
    SubmissionKind,
    User,
)
from .workflow.meetings import composition_check

CONTENT_KINDS = (SubmissionKind.INITIAL, SubmissionKind.RESUBMISSION)
FINAL_DECISIONS = {
    DecisionType.APPROVE, DecisionType.APPROVE_WITH_CONDITIONS, DecisionType.DISAPPROVE, DecisionType.EXEMPT_CONFIRM,
}


def _days(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 86400, 1)


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p90": None, "max": None}
    vs = sorted(values)
    p90 = vs[min(len(vs) - 1, int(round(0.9 * (len(vs) - 1))))]
    return {"n": len(vs), "mean": round(statistics.mean(vs), 1), "median": round(statistics.median(vs), 1),
            "p90": p90, "max": vs[-1]}


def initial_review_cycles(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    """신규 과제의 최초 접수부터 최종 결정(승인·면제·부결)까지의 주기."""
    cycles = []
    for p in db.scalars(select(Protocol)):
        subs = [s for s in p.submissions if s.kind in CONTENT_KINDS]
        if not subs:
            continue
        final = next((s for s in subs if s.decision and s.decision.decision in FINAL_DECISIONS), None)
        if final is None or not (start <= final.decision.decided_at.date() <= end):
            continue
        cycle = subs[: subs.index(final) + 1]
        irb = sum(_days(s.submitted_at, s.closed_at or s.decision.decided_at) for s in cycle if (s.closed_at or s.decision))
        researcher = 0.0
        for prev, nxt in zip(cycle, cycle[1:]):
            if prev.closed_at:
                researcher += _days(prev.closed_at, nxt.submitted_at)
        cycles.append({
            "protocol_no": p.protocol_no,
            "review_type": final.decision.review_type.value,
            "total": _days(cycle[0].submitted_at, final.decision.decided_at),
            "irb": round(irb, 1),
            "researcher": round(researcher, 1),
            "rounds": len(cycle),
            "decision": final.decision.decision.value,
        })
    return cycles


def kpi(db: Session, start: date, end: date) -> dict[str, Any]:
    in_range = lambda dt: dt is not None and start <= dt.date() <= end  # noqa: E731
    subs = [s for s in db.scalars(select(Submission)) if in_range(s.submitted_at)]
    decisions = [d for d in db.scalars(select(Decision)) if in_range(d.decided_at)]
    cycles = initial_review_cycles(db, start, end)

    by_type: dict[str, dict[str, Any]] = {}
    for rt in ("exempt", "expedited", "full"):
        cs = [c for c in cycles if c["review_type"] == rt]
        by_type[rt] = {
            "total": _stats([c["total"] for c in cs]),
            "irb": _stats([c["irb"] for c in cs]),
            "researcher": _stats([c["researcher"] for c in cs]),
            "first_round_rate": round(sum(c["rounds"] == 1 for c in cs) / len(cs) * 100, 1) if cs else None,
        }

    # 심의위원 업무량
    workload: dict[int, dict[str, Any]] = defaultdict(lambda: {"assigned": 0, "done": 0, "overdue": 0, "days": []})
    today_ = date.today()
    for a in db.scalars(select(ReviewAssignment)):
        if not in_range(a.assigned_at):
            continue
        w = workload[a.reviewer_id]
        w["assigned"] += 1
        if a.submitted_at:
            w["done"] += 1
            w["days"].append(_days(a.assigned_at, a.submitted_at))
            if a.due_date and a.submitted_at.date() > a.due_date:
                w["overdue"] += 1
        elif a.due_date and a.due_date < today_ and not a.submission.closed:
            w["overdue"] += 1
    reviewers = []
    for uid, w in workload.items():
        u = db.get(User, uid)
        reviewers.append({"name": u.full_name if u else uid, "assigned": w["assigned"], "done": w["done"],
                          "overdue": w["overdue"], "mean_days": round(statistics.mean(w["days"]), 1) if w["days"] else None})
    reviewers.sort(key=lambda r: -r["assigned"])

    # 자동 분류 정확도: 최초 사전검토 권고 vs 최종 결정 심의유형
    agree = total_cmp = 0
    confusion: Counter[tuple[str, str]] = Counter()
    for d in decisions:
        pr = d.submission.prereviews[0] if d.submission.prereviews else None
        if pr is None or d.submission.kind == SubmissionKind.FINAL_REPORT:
            continue
        total_cmp += 1
        confusion[(pr.review_type.value, d.review_type.value)] += 1
        agree += pr.review_type == d.review_type

    # 자주 발견되는 검토 의견(연구자 교육 자료로 활용)
    findings: Counter[str] = Counter()
    labels: dict[str, str] = {}
    for pr in db.scalars(select(PreReview)):
        if not in_range(pr.created_at):
            continue
        for f in pr.result.get("findings", []):
            if f["severity"] in ("critical", "major"):
                findings[f["code"]] += 1
                labels.setdefault(f["code"], f["message"])

    return {
        "period": {"start": start, "end": end},
        "submissions": len(subs),
        "submissions_by_kind": Counter(s.kind.label for s in subs),
        "submissions_by_type": Counter(s.review_type.label for s in subs if s.review_type),
        "decisions_by_type": Counter(d.decision.label for d in decisions),
        "cycles": cycles,
        "turnaround": by_type,
        "reviewers": reviewers,
        "classification": {
            "compared": total_cmp,
            "agreement_rate": round(agree / total_cmp * 100, 1) if total_cmp else None,
            "confusion": {f"{a}→{b}": n for (a, b), n in confusion.items()},
        },
        "top_findings": [{"code": c, "message": labels[c], "count": n} for c, n in findings.most_common(10)],
    }


def annual_report(db: Session, year: int) -> dict[str, Any]:
    start, end = date(year, 1, 1), date(year, 12, 31)
    k = kpi(db, start, end)
    meetings = [m for m in db.scalars(select(Meeting).order_by(Meeting.scheduled_at)) if m.scheduled_at.year == year]
    decisions = [d for d in db.scalars(select(Decision)) if d.decided_at.year == year]
    kinds = Counter(d.submission.kind.value for d in decisions)
    reports = [s for s in db.scalars(select(Submission)) if s.submitted_at.year == year
               and s.kind in (SubmissionKind.CONTINUING, SubmissionKind.FINAL_REPORT)]
    breaches = [s for s in reports if s.report.get("data_breach")]
    deviations = [s for s in reports if s.report.get("deviations")]
    active = [p for p in db.scalars(select(Protocol)) if p.is_active_approval]
    return {
        "year": year,
        "kpi": k,
        "meetings": meetings,
        "meeting_count": len([m for m in meetings if m.status == "closed"]),
        "composition": composition_check(db),
        "decisions_total": len(decisions),
        "initial_decisions": Counter(d.decision.label for d in decisions if d.submission.kind in CONTENT_KINDS),
        "by_review_type": Counter(d.review_type.label for d in decisions if d.submission.kind in CONTENT_KINDS),
        "amendments": kinds.get("amendment", 0),
        "continuing": kinds.get("continuing", 0),
        "final_reports": kinds.get("final_report", 0),
        "breaches": len(breaches),
        "deviations": len(deviations),
        "active_protocols": len(active),
    }
