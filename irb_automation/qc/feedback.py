"""Store AI-disputed rule findings and apply manually approved literal aliases."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Literal, TypedDict

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import audit
from ..ai.privacy import mask_text
from ..models import User
from ..timeutil import now
from .checks import criteria
from .models import QCRuleFeedback, QCRuleKeyword, QCRun
from .required import RequiredAliases


class Criterion(TypedDict):
    code: str
    doc_type: str
    label: str
    patterns: dict[str, list[str]]


class FeedbackConflict(Exception):
    """The requested review cannot be applied to the current case or rule."""


def required_criteria() -> dict[str, Criterion]:
    return {
        code: Criterion(code=code, doc_type=doc_type, label=item["label"],
                        patterns={kind: item[kind] for kind in ("any", "all", "must", "reject") if item.get(kind)})
        for doc_type, items in criteria().get("required", {}).items()
        for item in items
        for code in [f"REQ_{doc_type}_{item['id']}".upper()]
    }


def keyword_targets(criterion: Criterion) -> list[tuple[str, str]]:
    """Return the conditions that can receive a literal alternative."""
    targets = [("any", "")] if criterion["patterns"].get("any") else []
    return targets + [(kind, pattern) for kind in ("all", "must") for pattern in criterion["patterns"].get(kind, [])]


def active_aliases(db: Session) -> RequiredAliases:
    aliases: RequiredAliases = {}
    query = select(QCRuleKeyword.rule_code, QCRuleKeyword.kind, QCRuleKeyword.pattern, QCRuleKeyword.keyword).where(
        QCRuleKeyword.is_active.is_(True))
    for code, kind, pattern, keyword in db.execute(query):
        aliases.setdefault(code, {}).setdefault((kind, pattern), []).append(f"re:{re.escape(keyword)}")
    return aliases


def capture_rule_feedback(db: Session, run: QCRun) -> None:
    """Keep the first cited dispute for each finding, even when the run is repeated."""
    eligible = [(issue, issue.get("ai_review", {})) for issue in run.issues if issue.get("source") == "rule"]
    disputed = [(issue, ai) for issue, ai in eligible if ai.get("verdict") == "false_positive" and ai.get("locations")]
    if not disputed:
        return
    existing = set(db.scalars(select(QCRuleFeedback.fingerprint).where(QCRuleFeedback.run_id == run.id)))
    docs = {doc["key"]: doc["doc_type"] for doc in run.docs}
    for issue, ai in disputed:
        scope = sorted({loc["doc"] for loc in ai["locations"]})
        key = json.dumps([run.criteria_version, issue.get("code"), issue.get("title"), scope,
                          issue.get("field"), issue.get("evidence")], ensure_ascii=False)
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        if fingerprint in existing:
            continue
        existing.add(fingerprint)
        db.add(QCRuleFeedback(
            run_id=run.id, fingerprint=fingerprint, criteria_version=run.criteria_version,
            rule_code=issue.get("code", "")[:100], rule_id=issue.get("rule_id", "")[:20],
            doc_type=docs.get(issue.get("doc"), ""), severity=issue["severity"],
            title=mask_text(issue["title"]).text[:250], reason=mask_text(ai["reason"]).text[:300],
            outcome="dismissed" if issue["review"] == "dismissed" else "needs_check",
            citations=[{"doc": loc["doc"], "doc_type": docs.get(loc["doc"], ""),
                        "quote": mask_text(loc["quote"]).text[:200]} for loc in ai["locations"]],
        ))


def review_case(db: Session, actor: User, case: QCRuleFeedback, *,
                decision: Literal["applied", "reviewed", "rejected"], note: str,
                keyword: str = "", target: tuple[str, str] = ("", "")) -> None:
    """Commit a pending case decision and an optional alias in one transaction."""
    safe_note = mask_text(note.strip()).text[:1000]
    if not safe_note:
        raise FeedbackConflict("검토 메모를 입력해 주세요.")
    term = unicodedata.normalize("NFC", keyword.strip())
    if decision == "applied":
        criterion = required_criteria().get(case.rule_code)
        if criterion is None or target not in keyword_targets(criterion):
            raise FeedbackConflict("현재 규칙에서 추가할 매칭 조건을 확인해 주세요.")
        if not 2 <= len(term) <= 80 or not any(char.isalnum() for char in term) or mask_text(term).masked:
            raise FeedbackConflict("2~80자의 일반 문구를 입력해 주세요.")
    try:
        changed = db.scalar(update(QCRuleFeedback).where(QCRuleFeedback.id == case.id, QCRuleFeedback.status == "pending")
                            .values(status=decision, review_note=safe_note, reviewed_by=actor.id, reviewed_at=now())
                            .returning(QCRuleFeedback.id))
        if changed is None:
            raise FeedbackConflict("이미 검토된 사례입니다.")
        if decision == "applied":
            db.add(QCRuleKeyword(case_id=case.id, rule_code=case.rule_code, kind=target[0], pattern=target[1],
                                 keyword=term, keyword_key=term.casefold(), created_by=actor.id))
            db.flush()
    except IntegrityError as error:
        raise FeedbackConflict("이 규칙 조건에 같은 문구가 이미 등록되어 있습니다.") from error
    audit.record(db, actor, "qc_feedback_review", "qc_rule_feedback", case.id,
                 {"status": decision, "rule_code": case.rule_code})


def set_keyword_active(db: Session, actor: User, keyword_id: int, *, active: bool) -> bool:
    """Switch an approved alias on or off while retaining the case history."""
    changed = db.scalar(update(QCRuleKeyword).where(QCRuleKeyword.id == keyword_id, QCRuleKeyword.is_active != active)
                        .values(is_active=active).returning(QCRuleKeyword.id))
    if changed is None:
        return False
    audit.record(db, actor, "qc_keyword_toggle", "qc_rule_keyword", keyword_id, {"active": active})
    return True
