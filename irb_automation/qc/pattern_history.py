"""Record and reverse rule pattern changes with the matching state in one transaction."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import audit
from ..models import User
from .models import QCRuleKeyword
from .pattern_models import DocumentRuleChange, DocumentRulePattern

RulePattern = DocumentRulePattern | QCRuleKeyword


@dataclass(frozen=True, slots=True)
class StateChange:
    doc_type: str
    active: bool
    restored_change_id: int | None = None


def _history(actor: User, row: RulePattern, change: StateChange, before: bool | None) -> DocumentRuleChange:
    return DocumentRuleChange(
        pattern_id=row.id if isinstance(row, DocumentRulePattern) else None,
        alias_id=row.id if isinstance(row, QCRuleKeyword) else None,
        actor_id=actor.id, doc_type=change.doc_type, before_active=before, after_active=change.active,
        restored_change_id=change.restored_change_id,
    )


def record_registration(db: Session, actor: User, row: RulePattern, doc_type: str) -> None:
    db.add(_history(actor, row, StateChange(doc_type, True), None))


def change_active(db: Session, actor: User, row: RulePattern, change: StateChange) -> bool:
    """Persist an actual state transition and retain both its prior and new values."""
    model = type(row)
    changed = db.scalar(update(model).where(model.id == row.id, model.is_active != change.active)
                        .values(is_active=change.active).returning(model.id))
    if changed is None:
        return False
    db.add(_history(actor, row, change, not change.active))
    legacy = isinstance(row, QCRuleKeyword)
    action = "qc_keyword_toggle" if legacy else "document_rule_pattern_toggle"
    if change.restored_change_id is not None:
        action = "document_rule_pattern_restore"
    audit.record(db, actor, action, "qc_rule_keyword" if legacy else "document_rule_pattern",
                 row.id, {"active": change.active, "restored_change_id": change.restored_change_id})
    return True
