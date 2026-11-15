"""Persist public IRB requirement declarations and reproducible rule inputs."""
import hashlib
from datetime import date
from pathlib import Path

import yaml
from sqlalchemy import Date, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..database import Base
from ..engine.facts import ProtocolFacts
from ..engine.prereview import run_prereview
from ..engine.ruleset import Ruleset


class RequirementState(Base):
    __tablename__ = "requirement_states"

    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    status: Mapped[str] = mapped_column(String(25))
    declared_by: Mapped[int] = mapped_column(ForeignKey("users.id"))


class RulePack(Base):
    __tablename__ = "rule_packs"

    kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(80))
    content: Mapped[str] = mapped_column(Text)


class ReportReminder(Base):
    __tablename__ = "public_irb_report_reminders"

    protocol_id: Mapped[int] = mapped_column(ForeignKey("protocols.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    event_on: Mapped[date] = mapped_column(Date, primary_key=True)
    due_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    submitted_id: Mapped[int | None] = mapped_column(ForeignKey("submissions.id"), nullable=True)


def capture_packs(db: Session, paths: dict[str, Path]) -> dict[str, str]:
    refs = {}
    for kind, path in paths.items():
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if db.get(RulePack, (kind, sha)) is None:
            content = raw.decode("utf-8")
            version = str((yaml.safe_load(content) or {}).get("version", ""))
            db.add(RulePack(kind=kind, sha256=sha, version=version, content=content))
        refs[kind] = sha
    return refs


def replay(db: Session, basis: dict) -> dict | None:
    refs = basis.get("packs", {})
    inputs = basis.get("facts")
    if not inputs or not {"ruleset", "requirements"} <= refs.keys():
        return None
    rules = db.get(RulePack, ("ruleset", refs["ruleset"]))
    requirements = db.get(RulePack, ("requirements", refs["requirements"]))
    if rules is None or requirements is None:
        return None
    return run_prereview(ProtocolFacts.from_mapping(inputs),
                         Ruleset(yaml.safe_load(rules.content) or {}, rules.sha256),
                         requirements_content=requirements.content)
