"""Review transitions and explicitly selected cross-document corrections."""
from typing import TYPE_CHECKING, TypedDict

from .facts import DOSE_AMOUNT, Fact, restyle
from .rules import flag_meta
from .scoring import assess

if TYPE_CHECKING:
    from .models import QCRun


class Decision(TypedDict):
    action: str
    severity: str
    note: str


class CheckAnswer(TypedDict):
    answer: str
    note: str
    basis: str


CHECK_OPTIONS = {
    "scope_unclear": {"same_scope": "같은 범위이며 실제로 불일치", "different_scope": "비교 범위가 다름"},
    "alias_unknown": {"different_entity": "같은 역할의 서로 다른 대상", "same_entity": "같은 대상의 다른 이름"},
    "basis_unverified": {"applicable": "근거를 확인했으며 기준과 다름", "not_applicable": "기준이 적용되지 않음"},
    "review_pending": {"verified": "원문에서 오류를 직접 확인", "no_error": "오류가 아님"},
}
SEVERITY_RANK = {"minor": 0, "major": 1, "critical": 2}


class ReviewError(Exception):
    """An unsupported decision or reference selection."""


def resolve_check(issue: dict, decision: CheckAnswer) -> dict:
    """Record a human determination without confirming or scoring it."""
    answer, note, basis = decision["answer"], decision["note"].strip(), decision["basis"].strip()
    if (issue.get("review") != "candidate" or issue.get("result") != "needs_check"
            or issue.get("advice") or issue.get("merged_into")
            or answer not in CHECK_OPTIONS.get(issue.get("reason"), {})):
        raise ReviewError("이 확인 항목에 사용할 수 없는 답변입니다.")
    if not note or (answer == "applicable" and not basis):
        raise ReviewError("확인 사유와 해당하는 경우 적용 근거를 입력해 주세요.")
    excluded = answer in ("different_scope", "same_entity", "not_applicable", "no_error")
    result = "na" if excluded else issue.get("original_result", "mismatch")
    return dict(issue, result=result, reason="" if not excluded else note,
                review="dismissed" if excluded else "candidate", review_note=note,
                check_answer={"answer": answer, "note": note}, basis=basis or issue.get("basis", ""),
                question="", fixes=[], reference_blocked=True)


def transition(issue: dict, decision: Decision) -> dict:
    action, severity, note = decision["action"], decision["severity"], decision["note"].strip()
    state = issue.get("review")
    if issue.get("advice") or issue.get("merged_into") or not (
        (state == "candidate" and action in ("confirmed", "dismissed", "needs_check"))
        or (state == "confirmed" and issue["severity"] == "minor" and action == "accepted")
    ):
        raise ReviewError("현재 상태에서 선택할 수 없는 검토 조치입니다.")
    if action in ("dismissed", "accepted") and not note:
        raise ReviewError("검토 사유를 입력해 주세요.")
    updated = dict(issue, review=action, review_note=note)
    if action == "confirmed":
        if issue.get("result") == "needs_check" or (issue.get("confidence") == "ai_unverified" and not issue.get("check_answer")):
            raise ReviewError("판독·적용 범위 또는 근거가 확인되어야 확정할 수 있습니다.")
        flags = issue.get("flags", [])
        meta = flag_meta(issue.get("rule_id", ""), flags[0]) if flags else {}
        valid = {issue["severity"], meta.get("escalate") or issue["severity"]}
        downgrade = severity in SEVERITY_RANK and SEVERITY_RANK[severity] < SEVERITY_RANK[issue["severity"]]
        if downgrade and not note:
            raise ReviewError("등급을 낮추려면 사유를 입력해 주세요.")
        if severity and severity not in valid and not downgrade:
            raise ReviewError("규칙에서 허용한 등급만 선택할 수 있습니다.")
        updated["severity"] = severity or issue["severity"]
    if action == "needs_check":
        updated.update(review="candidate", result="needs_check", reason="review_pending",
                       original_result=issue.get("result", "mismatch"),
                       question=note or "원문과 적용 범위를 다시 확인해 주세요.", fixes=[])
    return updated


def select_reference(issue: dict, index: int) -> dict:
    locations = issue.get("locations", [])
    key = issue.get("fact_key")
    if (issue.get("category") != "cross" or issue.get("result") != "mismatch"
            or issue.get("reference_blocked")
            or issue.get("review") in ("dismissed", "resolved", "accepted")
            or not key or not 0 <= index < len(locations)):
        raise ReviewError("선택할 수 없는 기준값입니다.")
    chosen = locations[index]
    ref = Fact(key, chosen["norm"], chosen["value"])
    fixes = []
    for loc in locations:
        if loc["doc"] not in (chosen["doc"], "record") and loc["norm"] != ref.value and loc.get("unique"):
            new = restyle(key, loc["value"], ref)
            if new and new != loc["value"]:
                old = loc["value"]
                if key == "dosage":
                    old_amount = DOSE_AMOUNT.search(old)
                    new_amount = DOSE_AMOUNT.search(new)
                    if not old_amount or not new_amount or not loc.get("amount_unique"):
                        continue
                    old, new = old_amount[0], new_amount[0]
                fix = {"doc": loc["doc"], "old": old, "new": new}
                if fix not in fixes:
                    fixes.append(fix)
    return dict(issue, reference={"doc": chosen["doc"], "value": chosen["norm"]},
                fixes=fixes, targets=sorted({fx["doc"] for fx in fixes}))


def refresh(run: "QCRun") -> None:
    """Recompute the stored summary and document counts after a review."""
    from ..models import Document
    from ..timeutil import now

    states = {i["id"]: i.get("review") for i in run.issues}
    progress = []
    for check in run.checks or []:
        state = states.get(check.get("issue"))
        result = check["result"]
        if result != "na" and state in ("dismissed", "accepted", "resolved"):
            result = "match"
        elif result in ("missing", "mismatch") and state == "candidate":
            result = "candidate"
        progress.append(result)
    result = assess(run.issues, progress,
                    scorable=any(d["chars"] for d in run.docs))
    run.score, run.verdict = result["score"] or 0, result["status"]
    run.counts = {**result["counts"], "na": result["score"] is None,
                  **{k: result[k] for k in ("projected", "provisional", "pending", "coverage")}}
    by_id = {doc.id: doc for doc in run.protocol.documents} if run.protocol else {}
    for d in run.docs:
        doc: Document | None = by_id.get(d["document_id"])
        if doc is None:
            continue
        previous = (doc.validation or {}).get("qc", {})
        if previous.get("run", 0) > run.id:
            continue
        summary = assess([i for i in run.issues if i["doc"] == d["key"]
                          or any(loc["doc"] == d["key"] for loc in i.get("locations", []))])
        doc.validation = {**(doc.validation or {}), "qc": {
            "run": run.id, **summary["counts"], "status": summary["status"], "at": now().isoformat(timespec="minutes")}}
