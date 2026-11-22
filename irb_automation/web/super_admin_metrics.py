"""Period service metrics for the highest admin role: usage, uploads, review pipeline time, and LLM calls."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..metrics import AICall
from ..models import AuditLog, Document, User
from ..mvp.models import MvpUpload
from ..qc.models import QCRuleFeedback, QCRun
from ..timeutil import now

PERIODS = (7, 30, 90)
WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
PURPOSE_LABELS = {"knowledge_answer": "지식 질의응답", "-": "기타"}


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile; None when there is nothing to measure."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (pos - low), 2)


def spread(values: list[float]) -> dict:
    return {"n": len(values), "avg": round(sum(values) / len(values), 2) if values else None,
            "p50": percentile(values, 0.5), "p90": percentile(values, 0.9),
            "max": round(max(values), 2) if values else None}


def _ratio(part: int, whole: int) -> float | None:
    return round(part * 100 / whole, 1) if whole else None


def run_seconds(run: QCRun) -> float | None:
    """Processing time of a finished review; rows from before timing existed use creation → completion."""
    total = (run.timings or {}).get("total_s")
    if isinstance(total, (int, float)):
        return float(total)
    if run.status == "done" and run.stage_updated_at and run.created_at:
        return max(0.0, (run.stage_updated_at - run.created_at).total_seconds())
    return None


class _Days:
    """Per-day counters and sample lists for the selected window."""

    def __init__(self, start: date, days: int) -> None:
        self.keys = [(start + timedelta(days=i)).isoformat() for i in range(days)]
        self.counts: dict[str, Counter[str]] = {key: Counter() for key in self.keys}
        self.samples: dict[str, dict[str, list[float]]] = {key: defaultdict(list) for key in self.keys}
        self.active: dict[str, set[int]] = {key: set() for key in self.keys}

    def add(self, at: datetime | None, metric: str, amount: int = 1) -> None:
        if at is not None and (key := at.date().isoformat()) in self.counts:
            self.counts[key][metric] += amount

    def sample(self, at: datetime, metric: str, value: float) -> None:
        if (key := at.date().isoformat()) in self.samples:
            self.samples[key][metric].append(value)

    def seen(self, at: datetime, user_id: int | None) -> None:
        if user_id is not None and (key := at.date().isoformat()) in self.active:
            self.active[key].add(user_id)

    def rows(self) -> list[dict]:
        return [{"day": key, "active": len(self.active[key]),
                 **{name: self.counts[key][name] for name in ("uploads", "reviews", "failed", "logins",
                                                               "ai_calls", "ai_failed")},
                 "ai_ok": self.counts[key]["ai_calls"] - self.counts[key]["ai_failed"],
                 "pipeline_p50": percentile(self.samples[key]["pipeline"], 0.5),
                 "llm_p50": percentile(self.samples[key]["llm"], 0.5)} for key in self.keys]


def _uploads(db: Session, since: datetime, days: _Days, heat: list[list[int]]) -> tuple[dict, Counter[int]]:
    formats: Counter[str] = Counter()
    doc_types: Counter[str] = Counter()
    files_per_run: Counter[int] = Counter()
    accepted = rejected = size = 0
    for row in db.execute(select(MvpUpload.user_id, MvpUpload.created_at, MvpUpload.size, MvpUpload.fmt,
                                 MvpUpload.doc_type_label, MvpUpload.status, MvpUpload.run_id)
                          .where(MvpUpload.created_at >= since)):
        days.seen(row.created_at, row.user_id)
        days.add(row.created_at, "uploads")
        heat[row.created_at.weekday()][row.created_at.hour] += 1
        if row.status != "ok":
            rejected += 1
            continue
        accepted += 1
        size += row.size or 0
        formats[(row.fmt or "기타").upper()] += 1
        doc_types[row.doc_type_label or "기타"] += 1
        if row.run_id is not None:
            files_per_run[row.run_id] += 1
    for row in db.execute(select(Document.uploaded_by, Document.uploaded_at, Document.size, Document.filename)
                          .where(Document.uploaded_at >= since)):
        days.seen(row.uploaded_at, row.uploaded_by)
        days.add(row.uploaded_at, "uploads")
        accepted += 1
        size += row.size or 0
        formats[(row.filename.rsplit(".", 1)[-1] if "." in row.filename else "기타").upper()[:8]] += 1
    return {"accepted": accepted, "rejected": rejected, "bytes": size,
            "reject_rate": _ratio(rejected, accepted + rejected),
            "per_review": round(sum(files_per_run.values()) / len(files_per_run), 1) if files_per_run else None,
            "formats": formats.most_common(8), "doc_types": doc_types.most_common(8)}, files_per_run


def _reviews(runs: list[QCRun], days: _Days, heat: list[list[int]], files_per_run: Counter[int],
             llm_by_run: dict[int, float]) -> tuple[dict, dict]:
    statuses: Counter[str] = Counter(run.status for run in runs)
    verdicts: Counter[str] = Counter()
    scores: list[float] = []
    total: list[float] = []
    queue: list[float] = []
    per_doc: list[float] = []
    llm_share: list[float] = []
    stages: dict[str, list[float]] = {}
    ai_reviews = 0
    for run in runs:
        days.seen(run.created_at, run.created_by)
        heat[run.created_at.weekday()][run.created_at.hour] += 1
        if run.status == "failed":
            days.add(run.created_at, "failed")
        if run.status != "done":
            continue
        days.add(run.created_at, "reviews")
        verdicts[run.verdict] += 1
        scores.append(run.score)
        ai_reviews += bool(run.ai_used)
        seconds = run_seconds(run)
        if seconds is not None:
            total.append(seconds)
            days.sample(run.created_at, "pipeline", seconds)
            if files_per_run.get(run.id):
                per_doc.append(seconds / files_per_run[run.id])
            if seconds and run.id in llm_by_run:
                llm_share.append(min(100.0, llm_by_run[run.id] * 100 / seconds))
        timings = run.timings or {}
        if isinstance(timings.get("queue_s"), (int, float)):
            queue.append(float(timings["queue_s"]))
        for stage in timings.get("stages", []):
            stages.setdefault(stage["label"], []).append(float(stage["seconds"]))
    done = statuses.get("done", 0)
    reviews = {"total": len(runs), "done": done, "failed": statuses.get("failed", 0),
               "active": statuses.get("queued", 0) + statuses.get("running", 0),
               "success_rate": _ratio(done, done + statuses.get("failed", 0)), "ai_rate": _ratio(ai_reviews, done),
               "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
               "verdicts": sorted(verdicts.items(), key=lambda item: -item[1])}
    pipeline = {"total": spread(total), "queue": spread(queue), "per_doc": spread(per_doc),
                "llm_share": spread(llm_share),
                "stages": [{"label": label, **spread(values)} for label, values in stages.items()]}
    return reviews, pipeline


def _llm(calls: list[AICall], days: _Days) -> tuple[dict, dict[int, float]]:
    by_purpose: dict[str, list[AICall]] = defaultdict(list)
    models: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    by_run: dict[int, float] = defaultdict(float)
    for call in calls:
        by_purpose[call.purpose or "-"].append(call)
        models[call.model or "-"] += 1
        days.add(call.created_at, "ai_calls")
        if call.ok:
            days.sample(call.created_at, "llm", call.elapsed_ms / 1000)
        else:
            errors[call.error or "기타 오류"] += 1
            days.add(call.created_at, "ai_failed")
        if call.run_id is not None:
            by_run[call.run_id] += call.elapsed_ms / 1000
    purposes = sorted(({"purpose": PURPOSE_LABELS.get(name, name), "calls": len(items), "fail": sum(not c.ok for c in items),
                        "tokens": sum((c.input_tokens or 0) + (c.output_tokens or 0) for c in items),
                        **spread([c.elapsed_ms / 1000 for c in items if c.ok])}
                       for name, items in by_purpose.items()), key=lambda row: -row["calls"])
    in_tokens = sum(c.input_tokens or 0 for c in calls)
    out_tokens = sum(c.output_tokens or 0 for c in calls)
    measured = sum(c.output_tokens is not None for c in calls)
    ok = sum(c.ok for c in calls)
    return {"calls": len(calls), "failed": len(calls) - ok, "success_rate": _ratio(ok, len(calls)),
            "latency": spread([c.elapsed_ms / 1000 for c in calls if c.ok]),
            "input_tokens": in_tokens, "output_tokens": out_tokens,
            "tokens_per_call": round((in_tokens + out_tokens) / measured) if measured else None,
            "purposes": purposes, "models": models.most_common(6), "errors": errors.most_common()}, by_run


def dashboard(db: Session, period: int, today: date | None = None) -> dict:
    end = today or now().date()
    start = end - timedelta(days=period - 1)
    since = datetime.combine(start, datetime.min.time())
    days = _Days(start, period)
    heat = [[0] * 24 for _ in range(7)]

    logins = 0
    for actor, action, at in db.execute(select(AuditLog.actor_id, AuditLog.action, AuditLog.timestamp)
                                        .where(AuditLog.timestamp >= since, AuditLog.actor_id.is_not(None))):
        days.seen(at, actor)
        if action == "login":
            logins += 1
            days.add(at, "logins")
    uploads, files_per_run = _uploads(db, since, days, heat)
    llm, llm_by_run = _llm(list(db.scalars(select(AICall).where(AICall.created_at >= since))), days)
    runs = list(db.scalars(select(QCRun).where(QCRun.created_at >= since)))
    reviews, pipeline = _reviews(runs, days, heat, files_per_run, llm_by_run)

    active = set().union(*days.active.values())
    usage = {"active_users": len(active), "dau_today": len(days.active[end.isoformat()]),
             "dau_avg": round(sum(map(len, days.active.values())) / period, 1), "logins": logins,
             "new_users": db.scalar(select(func.count(User.id)).where(User.created_at >= since)) or 0,
             "reviews_per_user": round(reviews["done"] / len(active), 1) if active else None}
    return {"period": period, "start": start, "end": end, "usage": usage, "uploads": uploads, "reviews": reviews,
            "pipeline": pipeline, "llm": llm, "daily": days.rows(),
            "feedback": db.scalar(select(func.count(QCRuleFeedback.id))
                                  .where(QCRuleFeedback.created_at >= since)) or 0,
            "heatmap": {"rows": [{"day": WEEKDAYS[i], "hours": heat[i]} for i in range(7)],
                        "max": max(map(max, heat))}}
