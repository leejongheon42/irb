"""Import existing rules once and load the current database patterns for each run."""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import yaml
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..engine.ruleset import get_ruleset
from .pattern_models import DocumentRulePattern, DocumentRuleSettings
from .patterns import Expression
from .required import RequiredPatterns, required_groups

SETTINGS_ID = 1
current_settings: ContextVar[DocumentRuleSettings | None] = ContextVar("document_rule_settings", default=None)


class Target(NamedTuple):
    scope: str
    doc_type: str
    rule_code: str = ""
    condition: str = ""
    anchor: str = ""


@dataclass(frozen=True, slots=True)
class ClassificationPatterns:
    filename: dict[str, list[Expression]]
    body: dict[str, list[Expression]]
    structure: tuple[Expression, ...]


def pattern_key(value: str, format: str) -> str:
    key = value.casefold() if format == "literal" else value
    return hashlib.sha256(f"{format}:{key}".encode()).hexdigest()


@lru_cache(maxsize=1)
def _classification_seed() -> dict:
    return yaml.safe_load(Path(__file__).with_name("classification.yaml").read_text(encoding="utf-8"))


def default_classification() -> ClassificationPatterns:
    data = _classification_seed()
    body = {doc_type: [Expression(word, False, weight) for word, weight in words.items()]
            for doc_type, words in data["body"].items()}
    extras = get_ruleset().policy("document_pipeline").get("classify_keywords", {}) or {}
    for doc_type, words in extras.items():
        for value in words or []:
            word, weight = value if isinstance(value, (list, tuple)) and len(value) == 2 else (value, 5.0)
            body.setdefault(doc_type, []).append(Expression(str(word), False, float(weight)))
    return ClassificationPatterns(
        {doc_type: [Expression(word, False) for word in words] for doc_type, words in data["filename"].items()},
        body, tuple(Expression(word, False) for word in data["protocol_sections"]),
    )


def database_settings(db: Session) -> DocumentRuleSettings:
    return db.scalars(select(DocumentRuleSettings).where(DocumentRuleSettings.id == SETTINGS_ID)).one()


@contextmanager
def use_database_settings(db: Session) -> Iterator[None]:
    token = current_settings.set(database_settings(db))
    try:
        yield
    finally:
        current_settings.reset(token)


def active_classification(db: Session) -> ClassificationPatterns:
    rows = db.scalars(select(DocumentRulePattern).where(
        DocumentRulePattern.scope.in_(("filename", "body", "structure")),
        DocumentRulePattern.is_active.is_(True),
    ).order_by(DocumentRulePattern.priority, DocumentRulePattern.id))
    filename: dict[str, list[Expression]] = {}
    body: dict[str, list[Expression]] = {}
    structure: list[Expression] = []
    for row in rows:
        expression = Expression(row.expression, row.format == "regex", row.weight)
        match row.scope:
            case "filename":
                filename.setdefault(row.doc_type, []).append(expression)
            case "body":
                body.setdefault(row.doc_type, []).append(expression)
            case "structure":
                structure.append(expression)
    return ClassificationPatterns(filename, body, tuple(structure))


def active_required(db: Session) -> RequiredPatterns:
    patterns: RequiredPatterns = {}
    rows = db.scalars(select(DocumentRulePattern).where(
        DocumentRulePattern.scope == "required", DocumentRulePattern.is_active.is_(True),
    ).order_by(DocumentRulePattern.id))
    for row in rows:
        expression: str | Expression
        if row.created_by is None:
            expression = f"re:{row.expression}" if row.format == "regex" else row.expression
        else:
            expression = Expression(row.expression, row.format == "regex")
        patterns.setdefault(row.rule_code, {}).setdefault((row.condition, row.target), []).append(expression)
    return patterns


def new_pattern(target: Target, expression: str, format: str, *, weight: float = 1.0) -> DocumentRulePattern:
    return DocumentRulePattern(
        scope=target.scope, doc_type=target.doc_type, rule_code=target.rule_code,
        condition=target.condition, target=target.anchor,
        expression=expression, format=format, weight=weight, pattern_key=pattern_key(expression, format),
    )


def seed_patterns(db: Session) -> None:
    """Claim the import and insert all defaults in the same transaction."""
    if db.get(DocumentRuleSettings, SETTINGS_ID) is not None:
        return
    from .checks import criteria
    from .rules import rules

    config = criteria()
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else postgres_insert
    claim = insert(DocumentRuleSettings).values(id=SETTINGS_ID, criteria=config, detector_registry=rules())
    if db.scalar(claim.on_conflict_do_nothing(index_elements=["id"]).returning(DocumentRuleSettings.id)) is None:
        return
    defaults = default_classification()
    for scope, groups in (("filename", defaults.filename), ("body", defaults.body),
                          ("structure", {"protocol": defaults.structure})):
        for priority, (doc_type, expressions) in enumerate(groups.items()):
            # Repeated weighted terms in an institution's configuration previously added to the score.
            weights: dict[str, tuple[str, float]] = {}
            for expression in expressions:
                key = expression.value.casefold()
                value, weight = weights.get(key, (expression.value, 0.0))
                weights[key] = value, weight + expression.weight
            for value, weight in weights.values():
                row = new_pattern(Target(scope, doc_type), value, "literal", weight=weight)
                row.priority = priority
                db.add(row)
    for doc_type, items in config.get("required", {}).items():
        for item in items:
            code = f"REQ_{doc_type}_{item['id']}".upper()
            seen: set[tuple[str, str, str]] = set()
            for (condition, anchor), values in required_groups(item).items():
                for raw in values:
                    is_regex = raw.startswith("re:")
                    row = new_pattern(Target("required", doc_type, code, condition, anchor),
                                      raw[3:] if is_regex else raw, "regex" if is_regex else "literal")
                    key = (condition, anchor, row.pattern_key)
                    if key not in seen:
                        seen.add(key)
                        db.add(row)
