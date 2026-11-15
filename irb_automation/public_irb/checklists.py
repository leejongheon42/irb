"""Internal checklist preparation and explicit human decision transitions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..docproc.text import extract_text
from ..engine.facts import ProtocolFacts
from ..models import Protocol, Submission, User
from ..notifications import notify
from ..timeutil import now
from ..workflow import service as wf
from .checklist_models import (
    Answer,
    ChecklistDecision,
    ChecklistDefinition,
    ChecklistForm,
    ChecklistItem,
    ChecklistResponse,
    Direction,
)
from .source_models import SourceDocument

ANSWER_LABELS: Final = {"yes": "예", "no": "아니오", "na": "해당 없음", "unknown": "미확인", "pending": "검토 대기"}
DIRECTION_LABELS: Final = {
    "favorable": "항목 기준에 부합", "unfavorable": "검토 필요", "neutral": "별도 해석 필요",
    "na": "해당 없음", "unresolved": "미확정",
}


class ChecklistError(wf.WorkflowError):
    """The requested checklist action violates a decision or source boundary."""


@dataclass(frozen=True, slots=True)
class ChecklistRow:
    item: ChecklistItem
    response: ChecklistResponse | None
    candidate: Answer


def register_form(db: Session, user: User, definition: ChecklistDefinition) -> ChecklistForm:
    """Append a definition version; existing responses retain their original form."""
    wf.require_admin(user)
    source = db.get(SourceDocument, definition.source_document_id)
    if source is None:
        raise ChecklistError("원문을 찾을 수 없습니다.")
    if source.owner != definition.owner:
        raise ChecklistError("원문과 점검표의 작성 주체가 다릅니다.")
    if (source.series, source.number) not in {
        ("권고서식", "3"), ("권고서식", "11"), ("권고서식", "10-5"), ("권고서식", "10-6"),
        ("권고서식", "10-7"), ("권고서식", "10-8"), ("별지", "20"),
    }:
        raise ChecklistError("자가점검표 또는 평가점검표 원문을 선택하십시오.")
    if len({item.key for item in definition.items}) != len(definition.items):
        raise ChecklistError("섹션·번호 키는 중복될 수 없습니다.")
    if definition.reviewed and not source.verified:
        raise ChecklistError("원문 검수가 완료되어야 항목 정의를 확정할 수 있습니다.")
    form = ChecklistForm(source_document_id=source.id, owner=definition.owner,
                         items=[item.model_dump() for item in definition.items],
                         verified_by=user.id if definition.reviewed else None,
                         verified_at=now() if definition.reviewed else None)
    db.add(form)
    db.flush()
    audit.record(db, user, "checklist_form_create", "checklist_form", form.id,
                 {"source_document_id": source.id, "reviewed": definition.reviewed})
    return form


def draft_items(source: SourceDocument) -> list[ChecklistItem]:
    """Extract table rows only as unverified drafts, with no inferred direction."""
    if not source.stored_path:
        return []
    from .sources import source_bytes

    extracted = extract_text(source.original_filename, source_bytes(source))
    rows = [line.strip() for line in extracted.text.splitlines() if " | " in line]
    return [ChecklistItem(key=f"draft.{index}", label=line[:2000]) for index, line in enumerate(rows, 1)]


def can_decide(user: User, protocol: Protocol, form: ChecklistForm) -> bool:
    try:
        require_decider(user, protocol, form)
    except wf.PermissionDenied:
        return False
    return True


def require_decider(user: User, protocol: Protocol, form: ChecklistForm) -> None:
    """Check membership or assignment at every write, including conflict checks."""
    if form.owner == "researcher":
        wf.require_member(user, protocol)
    else:
        wf.require_reviewer(user, protocol)


def candidate(protocol: Protocol, item: ChecklistItem, submission: Submission | None = None) -> Answer:
    """Presence is a candidate only; absence and adequacy stay unconfirmed."""
    if submission is not None:
        uploaded = (submission.basis or {}).get("facts", {}).get("uploaded_doc_types", [])
        return "yes" if item.auto_doc_type in uploaded else "unknown"
    present = item.auto_doc_type and any(
        doc.doc_type == item.auto_doc_type and doc.is_current and doc.status in ("submitted", "accepted")
        for doc in protocol.documents
    )
    return "yes" if present else "unknown"


def applicable_forms(db: Session, facts: ProtocolFacts) -> list[ChecklistForm]:
    forms = db.scalars(select(ChecklistForm).order_by(ChecklistForm.id.desc())).all()
    selected: dict[tuple[str, str], ChecklistForm] = {}
    for form in forms:
        source = form.source
        if source.status == "archived" or not _applies(facts, source):
            continue
        key = (source.series, source.number)
        previous = selected.get(key)
        if previous is None or (form.verified and not previous.verified):
            selected[key] = form
    return list(selected.values())


def _applies(facts: ProtocolFacts, source: SourceDocument) -> bool:
    kind = facts.submission_kind
    if source.series == "권고서식":
        match source.number:
            case "3":
                return facts.application_route == "exemption" and kind in ("initial", "resubmission")
            case "11":
                return "waiver_request" in facts.consent_routes and kind in ("initial", "resubmission")
            case "10-5":
                return facts.research_category == "stem_cell" and kind in ("initial", "resubmission")
            case "10-6" | "10-7" | "10-8":
                return {"10-6": "info_provision", "10-7": "specimen_provision", "10-8": "disposal_transfer"}[source.number] == kind
    return kind in (source.applies_to or [])


def forms_for(db: Session, protocol: Protocol, submission: Submission | None) -> list[ChecklistForm]:
    if submission is not None and "checklists" in (submission.basis or {}):
        return [form for ref in submission.basis["checklists"] if (form := db.get(ChecklistForm, ref["id"])) is not None]
    facts = (ProtocolFacts.from_mapping(submission.snapshot, submission_kind=submission.kind.value) if submission
             else ProtocolFacts.from_protocol(protocol, submission_kind=wf.preview_kind(protocol)))
    return applicable_forms(db, facts)


def verified_for(form: ChecklistForm, submission: Submission | None) -> bool:
    pinned = (submission.basis or {}).get("checklists") if submission is not None else None
    if pinned is None:
        return form.verified
    return any(ref["id"] == form.id and ref["verified"] for ref in pinned)


def transition(item: ChecklistItem, decision: ChecklistDecision) -> Direction:
    """Interpret explicit answers using item polarity without granting approval."""
    if decision.answer in ("unknown", "pending"):
        return "unresolved"
    if not decision.evidence_ref.strip():
        raise ChecklistError("답변 확정에는 문서·쪽·항목 등 근거 위치 또는 해당 없음 사유가 필요합니다.")
    if decision.answer == "na":
        return "na"
    if item.favorable == "none":
        return "neutral"
    return "favorable" if decision.answer == item.favorable else "unfavorable"


def save_answer(db: Session, user: User, decision: ChecklistDecision) -> ChecklistResponse:
    protocol = db.get(Protocol, decision.protocol_id)
    form = db.get(ChecklistForm, decision.form_id)
    if protocol is None or form is None:
        raise ChecklistError("과제 또는 점검표를 찾을 수 없습니다.")
    require_decider(user, protocol, form)
    if protocol.committee != "public_irb":
        raise ChecklistError("공용 IRB 과제에서 사용하는 점검표입니다.")
    submission = db.get(Submission, decision.submission_id) if decision.submission_id else None
    if decision.submission_id and (submission is None or submission.protocol_id != protocol.id):
        raise ChecklistError("해당 과제의 제출 기록을 선택해 주세요.")
    if form not in forms_for(db, protocol, submission):
        raise ChecklistError("이 과제에 적용되는 점검표를 선택해 주세요.")
    item = next((ChecklistItem.model_validate(raw) for raw in form.items if raw["key"] == decision.item_key), None)
    if item is None:
        raise ChecklistError("점검표에 없는 항목입니다.")
    if not verified_for(form, submission) and decision.answer in ("yes", "no", "na"):
        raise ChecklistError("원문과 항목 정의가 미검수 상태이므로 답변을 확정할 수 없습니다.")
    direction = transition(item, decision)
    response = db.scalar(select(ChecklistResponse).where(
        ChecklistResponse.protocol_id == protocol.id,
        ChecklistResponse.form_id == form.id,
        ChecklistResponse.scope == (decision.submission_id or 0),
        ChecklistResponse.item_key == item.key,
    ))
    if response is None:
        response = ChecklistResponse(protocol_id=protocol.id, form_id=form.id, item_key=item.key,
                                     submission_id=decision.submission_id, scope=decision.submission_id or 0)
        db.add(response)
    response.answer = decision.answer
    response.direction = direction
    response.evidence_ref = decision.evidence_ref.strip()
    response.auto_candidate = candidate(protocol, item, submission)
    response.decided_by, response.decided_at = user.id, now()
    db.flush()
    audit.record(db, user, "checklist_answer", "checklist_response", response.id,
                 {"answer": response.answer, "direction": direction, "evidence_ref": response.evidence_ref})
    if form.owner == "committee" and direction != "unresolved":
        notify(db, protocol.member_ids(), "내부 평가점검표 판정이 기록되었습니다.",
               f"/protocols/{protocol.id}/public-irb" + (f"?submission_id={submission.id}" if submission else ""),
               dedup_key=f"checklist-reviewed:{protocol.id}:{response.scope}:{form.id}")
    return response


def rows(protocol: Protocol, form: ChecklistForm, responses: list[ChecklistResponse],
         submission: Submission | None = None) -> list[ChecklistRow]:
    by_key = {response.item_key: response for response in responses}
    items = [ChecklistItem.model_validate(raw) for raw in form.items]
    return [ChecklistRow(item, by_key.get(item.key), candidate(protocol, item, submission)) for item in items]
