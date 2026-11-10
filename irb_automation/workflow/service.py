"""연구자 과제 진행 관리(외부 IRB 제출 추적).

심의는 병원 IRB(e-IRB)에서 이뤄지며, 이 시스템은 연구자가 서류를 자동 완성·점검하고
제출·보완·결과를 기록하도록 돕는다.

  작성중 ──제출 기록──▶ IRB 제출함 ──결과 입력──▶ 승인/시정승인/면제 ─┬─▶ 변경 준비중 ─제출 기록─▶ ...
    ▲                     │                                        ├─▶ 지속심의 제출 ─▶ 승인(기간연장)
    └── 보완요청 받음 ◀────┘(보완 요청 입력)                         └─▶ 종료보고 제출 ─▶ 종료
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..engine.facts import PROTOCOL_FIELDS, ProtocolFacts, snapshot_protocol
from ..engine.prereview import run_prereview
from ..models import (
    SUBMISSION_RESULTS,
    Comment,
    PreReview,
    Protocol,
    ProtocolStatus,
    ReviewType,
    RevisionItem,
    Role,
    Submission,
    SubmissionKind,
    User,
)
from ..notifications import notify
from ..timeutil import now, today

S = ProtocolStatus


class WorkflowError(Exception):
    """업무 규칙 위반."""


class PermissionDenied(WorkflowError):
    pass


# ---------------------------------------------------------------------- 권한
def is_member(user: User, protocol: Protocol) -> bool:
    return user.id in protocol.member_ids()


def can_view(db: Session, user: User, protocol: Protocol) -> bool:
    return user.is_admin or is_member(user, protocol)


def require_admin(user: User) -> None:
    if not user.is_admin:
        raise PermissionDenied("관리자 권한이 필요합니다.")


def require_member(user: User, protocol: Protocol) -> None:
    if not is_member(user, protocol):
        raise PermissionDenied("연구책임자 또는 공동연구자만 수행할 수 있습니다.")


# ---------------------------------------------------------------------- 과제
EDITABLE_FIELDS = set(PROTOCOL_FIELDS) | {"co_investigators"}


def create_protocol(db: Session, user: User, data: dict[str, Any], ip: str = "") -> Protocol:
    if user.role not in (Role.RESEARCHER, Role.ADMIN):
        raise PermissionDenied("과제 생성 권한이 없습니다.")
    protocol = Protocol(pi_id=user.id, title=data.get("title") or "(제목 없음)")
    _apply(protocol, data)
    db.add(protocol)
    db.flush()
    audit.record(db, user, "protocol_create", "protocol", protocol.id, {"title": protocol.title}, ip)
    return protocol


def update_protocol(db: Session, user: User, protocol: Protocol, data: dict[str, Any], ip: str = "") -> None:
    require_member(user, protocol)
    if not protocol.is_editable:
        raise WorkflowError(f"'{protocol.status.label}' 상태에서는 수정할 수 없습니다.")
    changed = _apply(protocol, data)
    if changed:
        audit.record(db, user, "protocol_update", "protocol", protocol.id, {"fields": sorted(changed)}, ip)


def _apply(protocol: Protocol, data: dict[str, Any]) -> set[str]:
    changed: set[str] = set()
    for key, value in data.items():
        if key not in EDITABLE_FIELDS:
            continue
        if key == "custom_fields":
            # 현재 양식에 정의되지 않은 항목(과거 양식 값)은 지우지 않고 보존
            defined = set(data.get("_custom_defined") or (value or {}).keys())
            kept = {k: v for k, v in (protocol.custom_fields or {}).items() if k not in defined}
            value = {**kept, **(value or {})}
        if getattr(protocol, key) != value:
            setattr(protocol, key, value)
            changed.add(key)
    return changed


def _restore_snapshot(protocol: Protocol, snapshot: dict[str, Any]) -> None:
    for key in PROTOCOL_FIELDS:
        if key not in snapshot:
            continue
        value = snapshot[key]
        if key in ("study_start", "study_end", "data_period_start", "data_period_end") and value:
            value = date.fromisoformat(value)
        setattr(protocol, key, value)


# ---------------------------------------------------------------------- 사전검토
def build_facts(
    db: Session, protocol: Protocol, kind: str = "initial", report: dict[str, Any] | None = None
) -> ProtocolFacts:
    pi = protocol.pi or db.get(User, protocol.pi_id)
    previous = None
    for sub in reversed(protocol.submissions):
        if sub.review_type and sub.kind != SubmissionKind.CONTINUING:
            previous = sub.review_type.value
            break
    return ProtocolFacts.from_protocol(
        protocol,
        submission_kind=kind,
        previous_review_type=previous,
        uploaded_doc_types={d.doc_type for d in protocol.documents},
        pi_training_expires_on=pi.training_expires_on if pi else None,
        team_training=[
            {"name": u.full_name, "expires_on": u.training_expires_on}
            for u in (db.get(User, int(uid)) for uid in protocol.co_investigators or [])
            if u is not None
        ],
        protocol_expires_at=protocol.expires_at,
        report=report or {},
    )


def next_submission_kind(protocol: Protocol) -> SubmissionKind:
    if protocol.status == S.DRAFT:
        return SubmissionKind.INITIAL
    if protocol.status == S.AMENDMENT or (
        protocol.status == S.REVISION_REQUESTED and protocol.approved_at is not None
    ):
        return SubmissionKind.AMENDMENT
    if protocol.status == S.REVISION_REQUESTED:
        return SubmissionKind.RESUBMISSION
    raise WorkflowError(f"'{protocol.status.label}' 상태에서는 제출할 수 없습니다.")


def preview(db: Session, protocol: Protocol, kind: str | None = None, report: dict[str, Any] | None = None) -> dict[str, Any]:
    """저장하지 않고 사전검토 결과만 계산(작성 중 실시간 점검용)."""
    if kind is None:
        try:
            kind = next_submission_kind(protocol).value
        except WorkflowError:
            kind = "initial"
    return run_prereview(build_facts(db, protocol, kind, report))


def _run_and_store_prereview(db: Session, submission: Submission, facts: ProtocolFacts) -> PreReview:
    result = run_prereview(facts)
    pr = PreReview(
        submission=submission,
        review_type=ReviewType(result["review_type"]),
        risk_score=result["risk"]["score"],
        risk_level=result["risk"]["level"],
        result=result,
        ruleset_version=result["ruleset_version"],
    )
    db.add(pr)
    submission.review_type = pr.review_type
    return pr


# ---------------------------------------------------------------------- 제출 기록
def submission_check(db: Session, protocol: Protocol, kind: SubmissionKind | None = None) -> dict[str, Any]:
    """제출 전 자동 사전점검 결과(IRB 예상 심의유형·필수 보완 사항)."""
    kind = kind or next_submission_kind(protocol)
    return run_prereview(build_facts(db, protocol, kind.value))


def record_submission(
    db: Session, user: User, protocol: Protocol, *, external_ref: str = "", submitted_on: date | None = None,
    force: bool = False, ip: str = "",
) -> Submission:
    """외부 IRB(e-IRB)에 제출했음을 기록한다. 필수 보완 사항이 남아 있으면 force 없이는 거부."""
    require_member(user, protocol)
    kind = next_submission_kind(protocol)
    unanswered = [i for i in open_revision_items(db, protocol) if not i.response_text.strip()]
    if protocol.status == S.REVISION_REQUESTED and unanswered and not force:
        raise WorkflowError(f"보완 요청 {len(unanswered)}개 항목에 대한 답변을 먼저 작성하십시오.")
    facts = build_facts(db, protocol, kind.value)
    result = run_prereview(facts)
    if not result["ready_for_review"] and not force:
        crit = [f["message"] for f in result["findings"] if f["severity"] == "critical"]
        raise WorkflowError("필수 보완 사항이 남아 있습니다(그래도 기록하려면 '보완 없이 기록'을 선택): "
                            + " / ".join(crit[:5]))
    return _create_submission(db, user, protocol, kind, facts, external_ref=external_ref,
                              submitted_on=submitted_on, ip=ip)


def submit_report(
    db: Session, user: User, protocol: Protocol, kind: SubmissionKind, report: dict[str, Any], ip: str = "",
    external_ref: str = "",
) -> Submission:
    """지속심의 신청 또는 종료보고 제출 기록."""
    require_member(user, protocol)
    allowed = {
        SubmissionKind.CONTINUING: (S.APPROVED, S.EXPIRED),
        SubmissionKind.FINAL_REPORT: (S.APPROVED, S.EXEMPT, S.EXPIRED),
    }
    if kind not in allowed:
        raise WorkflowError("지원하지 않는 보고 유형입니다.")
    if protocol.status not in allowed[kind]:
        raise WorkflowError(f"'{protocol.status.label}' 상태에서는 {kind.label}을(를) 제출할 수 없습니다.")
    facts = build_facts(db, protocol, kind.value, report)
    return _create_submission(db, user, protocol, kind, facts, report=report, external_ref=external_ref, ip=ip)


def _create_submission(
    db: Session, user: User, protocol: Protocol, kind: SubmissionKind, facts: ProtocolFacts,
    report: dict[str, Any] | None = None, external_ref: str = "", submitted_on: date | None = None, ip: str = "",
) -> Submission:
    prior_status = protocol.status.value
    if kind == SubmissionKind.AMENDMENT:
        protocol.version += 1
    when = now() if submitted_on is None else now().replace(year=submitted_on.year, month=submitted_on.month,
                                                              day=submitted_on.day)
    sub = Submission(
        protocol=protocol, kind=kind, version=protocol.version, snapshot=snapshot_protocol(protocol),
        report=report or {}, submitted_by=user.id, submitted_at=when,
        prior_status=_prior_approval_status(protocol, prior_status),
        external_ref=external_ref.strip(),
    )
    db.add(sub)
    pr = _run_and_store_prereview(db, sub, facts)
    if kind not in (SubmissionKind.CONTINUING, SubmissionKind.FINAL_REPORT):
        protocol.review_type = pr.review_type
        protocol.risk_score = pr.risk_score
        protocol.risk_level = pr.risk_level
    protocol.status = S.SUBMITTED
    db.flush()
    audit.record(db, user, "submit", "protocol", protocol.id,
                 {"kind": kind.value, "version": protocol.version, "external_ref": sub.external_ref,
                  "expected_review_type": pr.review_type.value}, ip)
    return sub


def _prior_approval_status(protocol: Protocol, current: str) -> str | None:
    """변경·지속·종료 제출이 부결될 때 되돌아갈 승인 상태."""
    if protocol.approved_at is None:
        return None
    if current in (S.APPROVED.value, S.EXEMPT.value, S.EXPIRED.value):
        return current
    return _last_approved_status(protocol)


def _last_approved_status(protocol: Protocol) -> str:
    for sub in reversed(protocol.submissions):
        if sub.result == "exempt":
            return S.EXEMPT.value
        if sub.result in ("approved", "approved_with_conditions"):
            return S.APPROVED.value
    return S.APPROVED.value


# ---------------------------------------------------------------------- 보완 요청(외부 IRB → 연구자)
def _split_revision_text(text: str) -> list[str]:
    """IRB 보완 요청문을 항목별로 나눈다(1. / 1) / ① / - / • 등 번호·글머리 기준)."""
    import re

    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    marker = re.compile(r"^\s*(?:\d{1,2}[.)]|[①-⑳]|[-•·▪]|\(\d{1,2}\)|[가-하][.)])\s+")
    items: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        if marker.match(line) or not items:
            items.append(marker.sub("", line).strip())
        else:
            items[-1] += " " + line.strip()
    return [i for i in items if i]


def record_revision_request(
    db: Session, user: User, protocol: Protocol, text: str, ip: str = "",
) -> list[RevisionItem]:
    """IRB에서 받은 보완 요청을 입력한다. 항목별로 나눠 답변 대상으로 만든다."""
    require_member(user, protocol)
    sub = protocol.current_submission
    if protocol.status != S.SUBMITTED or sub is None:
        raise WorkflowError("IRB에 제출한 신청건에만 보완 요청을 입력할 수 있습니다.")
    parts = _split_revision_text(text)
    if not parts:
        raise WorkflowError("보완 요청 내용을 입력하십시오.")
    protocol.status = S.REVISION_REQUESTED
    sub.result = "revision"
    sub.result_date = today()
    sub.result_note = text.strip()
    _close_submission(sub, "revision_requested")
    created = []
    for seq, part in enumerate(parts, start=1):
        item = RevisionItem(protocol_id=protocol.id, submission_id=sub.id, seq=seq, finding_code="",
                            request_text=part, created_by=user.id)
        db.add(item)
        created.append(item)
    db.flush()
    audit.record(db, user, "request_revision", "protocol", protocol.id, {"items": len(created)}, ip)
    notify(db, [m for m in protocol.member_ids() if m != user.id],
           f"{protocol.title[:40]} 보완 요청 {len(created)}건 입력됨", f"/protocols/{protocol.id}")
    return created


def _close_submission(sub: Submission, outcome: str) -> None:
    sub.closed = True
    sub.closed_at = now()
    sub.outcome = outcome


def open_revision_items(db: Session, protocol: Protocol) -> list[RevisionItem]:
    """가장 최근 보완 요청의 항목(답변 대상)."""
    latest = db.scalars(
        select(RevisionItem.submission_id).where(RevisionItem.protocol_id == protocol.id)
        .order_by(RevisionItem.id.desc()).limit(1)
    ).first()
    if latest is None:
        return []
    return list(db.scalars(
        select(RevisionItem).where(RevisionItem.submission_id == latest).order_by(RevisionItem.seq)
    ))


def revision_items_for(db: Session, submission_id: int) -> list[RevisionItem]:
    return list(db.scalars(
        select(RevisionItem).where(RevisionItem.submission_id == submission_id).order_by(RevisionItem.seq)
    ))


def respond_revision_items(
    db: Session, user: User, protocol: Protocol, responses: dict[int, str], ip: str = ""
) -> int:
    require_member(user, protocol)
    if protocol.status != S.REVISION_REQUESTED:
        raise WorkflowError("보완요청 상태에서만 답변할 수 있습니다.")
    items = {i.id: i for i in open_revision_items(db, protocol)}
    changed = 0
    for iid, text in responses.items():
        item = items.get(iid)
        if item is None:
            continue
        text = text.strip()
        if text != item.response_text:
            item.response_text = text
            item.responded_at = now() if text else None
            item.status = "answered" if text else "open"
            changed += 1
    if changed:
        audit.record(db, user, "revision_response", "protocol", protocol.id, {"answered": changed}, ip)
    return changed


# ---------------------------------------------------------------------- IRB 결과 입력
RESULTS_BY_KIND: dict[SubmissionKind, tuple[str, ...]] = {
    SubmissionKind.INITIAL: ("approved", "approved_with_conditions", "exempt", "disapproved"),
    SubmissionKind.RESUBMISSION: ("approved", "approved_with_conditions", "exempt", "disapproved"),
    SubmissionKind.AMENDMENT: ("approved", "approved_with_conditions", "exempt", "disapproved"),
    SubmissionKind.CONTINUING: ("approved", "approved_with_conditions", "disapproved"),
    SubmissionKind.FINAL_REPORT: ("acknowledged",),
}


def record_result(
    db: Session, user: User, protocol: Protocol, result: str, *, result_date: date | None = None,
    approval_end: date | None = None, irb_no: str = "", note: str = "", ip: str = "",
) -> Submission:
    """IRB 통지서의 결과를 입력한다(승인·시정승인·면제·부결·종료 확인). 보완 요청은 record_revision_request."""
    require_member(user, protocol)
    sub = protocol.current_submission
    if sub is None or sub.closed or protocol.status != S.SUBMITTED:
        raise WorkflowError("결과를 입력할 제출 기록이 없습니다.")
    if result not in RESULTS_BY_KIND[sub.kind]:
        raise WorkflowError(f"{sub.kind.label}에는 '{SUBMISSION_RESULTS.get(result, result)}' 결과를 입력할 수 없습니다.")
    t = result_date or today()
    if result in ("approved", "approved_with_conditions"):
        if approval_end is None:
            base = protocol.expires_at if sub.kind == SubmissionKind.CONTINUING and protocol.expires_at else t
            approval_end = base + timedelta(days=get_settings().approval_validity_days)
            if protocol.study_end and protocol.study_end >= t:
                approval_end = min(approval_end, protocol.study_end)
        if sub.kind not in (SubmissionKind.CONTINUING, SubmissionKind.AMENDMENT) or protocol.approved_at is None:
            protocol.approved_at = t
        protocol.expires_at = approval_end
        protocol.status = S.APPROVED
    elif result == "exempt":
        protocol.approved_at = protocol.approved_at or t
        protocol.expires_at = approval_end or protocol.study_end
        protocol.status = S.EXEMPT
    elif result == "disapproved":
        if sub.kind in (SubmissionKind.AMENDMENT, SubmissionKind.CONTINUING) and sub.prior_status:
            last_ok = _last_approved_submission(protocol, exclude=sub)
            if last_ok and sub.kind == SubmissionKind.AMENDMENT:
                _restore_snapshot(protocol, last_ok.snapshot)
                protocol.version = last_ok.version
            protocol.status = S(sub.prior_status)
        else:
            protocol.status = S.DISAPPROVED
    elif result == "acknowledged":
        protocol.status = S.CLOSED
    if irb_no.strip():
        protocol.protocol_no = irb_no.strip()
    sub.result = result
    sub.result_date = t
    sub.approval_end = approval_end if result in ("approved", "approved_with_conditions", "exempt") else None
    sub.result_note = note.strip()
    _close_submission(sub, "decided")
    audit.record(db, user, "decision", "protocol", protocol.id,
                 {"result": result, "kind": sub.kind.value,
                  "approval_end": approval_end.isoformat() if approval_end else None}, ip)
    return sub


def _last_approved_submission(protocol: Protocol, exclude: Submission | None) -> Submission | None:
    for sub in reversed(protocol.submissions):
        if sub is exclude:
            continue
        if sub.result in ("approved", "approved_with_conditions", "exempt") and sub.kind not in (
            SubmissionKind.CONTINUING, SubmissionKind.FINAL_REPORT
        ):
            return sub
    return None


def start_amendment(db: Session, user: User, protocol: Protocol, ip: str = "") -> None:
    require_member(user, protocol)
    if protocol.status not in (S.APPROVED, S.EXEMPT) or not protocol.is_active_approval:
        raise WorkflowError("유효한 승인 상태에서만 변경 신청을 시작할 수 있습니다.")
    protocol.status = S.AMENDMENT
    audit.record(db, user, "start_amendment", "protocol", protocol.id, {}, ip)


def cancel_amendment(db: Session, user: User, protocol: Protocol, ip: str = "") -> None:
    require_member(user, protocol)
    if protocol.status != S.AMENDMENT:
        raise WorkflowError("변경 준비중 상태가 아닙니다.")
    last_ok = _last_approved_submission(protocol, exclude=None)
    if last_ok:
        _restore_snapshot(protocol, last_ok.snapshot)
        protocol.status = S.EXEMPT if last_ok.result == "exempt" else S.APPROVED
    else:
        protocol.status = S.APPROVED
    audit.record(db, user, "status_change", "protocol", protocol.id, {"cancel_amendment": True}, ip)


def add_comment(db: Session, user: User, protocol: Protocol, body: str, ip: str = "") -> Comment:
    if not body.strip():
        raise WorkflowError("내용을 입력하십시오.")
    c = Comment(protocol_id=protocol.id, author_id=user.id, body=body.strip())
    db.add(c)
    db.flush()
    audit.record(db, user, "comment", "protocol", protocol.id, {"comment_id": c.id}, ip)
    targets = set(protocol.member_ids())
    targets.discard(user.id)
    notify(db, targets, f"{protocol.protocol_no or protocol.title} 연구팀 메모: {body.strip()[:60]}", f"/protocols/{protocol.id}")
    return c


# ---------------------------------------------------------------------- 스케줄러
def run_scheduler(db: Session) -> dict[str, int]:
    """승인 만료 처리와 만료 예정(지속심의 준비) 알림. 매일 1회 이상 실행."""
    settings = get_settings()
    t = today()
    stats = {"expired": 0, "expiry_notices": 0}
    active = db.scalars(
        select(Protocol).where(Protocol.status.in_([S.APPROVED, S.AMENDMENT]), Protocol.expires_at.is_not(None))
    ).all()
    for p in active:
        assert p.expires_at is not None
        if p.expires_at < t and p.status == S.APPROVED:
            p.status = S.EXPIRED
            stats["expired"] += 1
            audit.record(db, None, "expire", "protocol", p.id, {"expires_at": p.expires_at.isoformat()})
            notify(db, p.member_ids(),
                   f"{p.protocol_no or p.title[:30]} 승인 기간 만료 - 연구를 중단하고 지속심의 또는 종료보고를 준비하십시오.",
                   f"/protocols/{p.id}", dedup_key=f"expired:{p.id}:{p.expires_at}")
            continue
        days_left = (p.expires_at - t).days
        for notice in sorted(settings.expiry_notice_days):
            if 0 <= days_left <= notice:
                stats["expiry_notices"] += notify(
                    db, p.member_ids(),
                    f"{p.protocol_no or p.title[:30]} 승인 만료 {days_left}일 전({p.expires_at.isoformat()}) - 지속심의 서류를 준비하십시오.",
                    f"/protocols/{p.id}/package?kind=continuing", dedup_key=f"expiry:{p.id}:{p.expires_at}:{notice}",
                )
                break
    return stats
