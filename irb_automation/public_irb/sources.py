"""Content-addressed originals, composite identity lookup, and activation gates."""
from __future__ import annotations

import hashlib
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..docproc.text import extract_text
from ..timeutil import now, today
from .source_cover import CoverIdentity, check_cover
from .source_models import SourceDocument


class SourceError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class SourceKey(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)
    series: Literal["권고서식", "별지", "공용위원회 별지", "시행규칙 별지", "지침", "조회자료"]
    number: str = Field(min_length=1, max_length=50)
    version: str = Field(min_length=1, max_length=50)


class SourceRegistration(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)
    key: SourceKey
    title: str = Field(min_length=1, max_length=255)
    issuer: str = "공용기관생명윤리위원회"
    owner: Literal["researcher", "committee", "reference"] = "researcher"
    origin: str = Field(min_length=1)
    notion_block_id: str = ""
    original_filename: str = Field(min_length=1, max_length=255)
    applies_to: tuple[str, ...] = ()
    reference_date: date | None = None


class SourceVerification(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)
    verified_by: str = Field(min_length=1)
    verified_at: datetime = Field(default_factory=now)
    manual_note: str = ""


def get_source(db: Session, key: SourceKey) -> SourceDocument | None:
    """Require series, number and edition together; a bare number is never a key."""
    return db.scalar(select(SourceDocument).where(
        SourceDocument.series == key.series,
        SourceDocument.number == key.number,
        SourceDocument.version == key.version,
    ))


def register_source(db: Session, registration: SourceRegistration, raw: bytes) -> SourceDocument:
    """Acquire original bytes once per identity; never overwrite a published edition."""
    if not raw:
        raise SourceError("empty_source")
    digest = hashlib.sha256(raw).hexdigest()
    key = registration.key
    existing = get_source(db, key)
    if existing and existing.sha256:
        if existing.sha256 != digest:
            raise SourceError("edition_content_conflict")
        return existing
    directory = get_settings().data_dir / "public_irb" / "sources"
    directory.mkdir(parents=True, exist_ok=True)
    stored = directory / digest
    if stored.exists():
        if hashlib.sha256(stored.read_bytes()).hexdigest() != digest:
            raise SourceError("stored_source_integrity")
    else:
        stored.write_bytes(raw)
    extracted = extract_text(registration.original_filename, raw)
    document = existing or SourceDocument(**key.model_dump())
    document.title = registration.title
    document.issuer = registration.issuer
    document.owner = registration.owner
    document.origin = registration.origin
    document.notion_block_id = registration.notion_block_id
    document.original_filename = Path(registration.original_filename).name
    document.reference_date = registration.reference_date
    document.applies_to = list(registration.applies_to)
    document.sha256 = digest
    document.stored_path = str(stored)
    document.acquired_on = today()
    document.status = "unverified"
    document.cover_check = check_cover(CoverIdentity(key.series, key.number, key.version), extracted)
    db.add(document)
    db.flush()
    return document


def source_bytes(document: SourceDocument) -> bytes:
    """Read the acquired source and enforce its content-addressed integrity."""
    if not document.stored_path or not document.sha256:
        raise SourceError("source_not_acquired")
    stored = Path(document.stored_path).resolve()
    directory = (get_settings().data_dir / "public_irb" / "sources").resolve()
    if stored.parent != directory or stored.name != document.sha256:
        raise SourceError("source_path_outside_storage")
    try:
        raw = stored.read_bytes()
    except FileNotFoundError as exc:
        raise SourceError("source_not_acquired") from exc
    if hashlib.sha256(raw).hexdigest() != document.sha256:
        raise SourceError("stored_source_integrity")
    return raw


def verify_source(db: Session, document: SourceDocument, verification: SourceVerification) -> SourceDocument:
    """Record the human review; uncertain covers require a documented manual check."""
    source_bytes(document)
    if document.cover_check != "matched" and not verification.manual_note:
        raise SourceError("manual_verification_required")
    document.verified_by = verification.verified_by
    document.verified_at = verification.verified_at
    document.verification_note = verification.manual_note
    db.flush()
    return document


def activate_source(db: Session, document: SourceDocument) -> SourceDocument:
    """Activate a verified edition and retain the previous current edition as history."""
    if not document.verified_by or document.verified_at is None:
        raise SourceError("verification_required")
    if document.version == "확인 대기":
        raise SourceError("edition_verification_required")
    if document.cover_check != "matched" and not document.verification_note:
        raise SourceError("manual_verification_required")
    source_bytes(document)
    previous = db.scalars(select(SourceDocument).where(
        SourceDocument.series == document.series,
        SourceDocument.number == document.number,
        SourceDocument.status == "current",
        SourceDocument.id != document.id,
    ))
    for item in previous:
        item.status = "archived"
    document.status = "current"
    db.flush()
    return document


def seed_catalog(db: Session) -> list[SourceDocument]:
    """Register discovery metadata only; supplied references confer no verified status."""
    from .source_catalog import SOURCE_CATALOG

    documents = []
    for entry in SOURCE_CATALOG:
        existing = get_source(db, entry.key)
        if existing:
            documents.append(existing)
            continue
        document = SourceDocument(
            **entry.key.model_dump(), title=entry.title, issuer=entry.issuer, owner=entry.owner,
            origin=entry.origin, original_filename=entry.original_filename,
            applies_to=list(entry.applies_to), reference_date=entry.reference_date,
        )
        db.add(document)
        documents.append(document)
    db.flush()
    return documents


def agreement_lookup(db: Session, institution: str) -> tuple[SourceDocument | None, bool]:
    source = db.scalar(select(SourceDocument).where(
        SourceDocument.series == "조회자료", SourceDocument.number == "agreement-list",
        SourceDocument.status == "current",
    ))
    if source is None or not source.verified:
        return None, False
    try:
        extracted = extract_text(source.original_filename, source_bytes(source))
    except SourceError:
        return None, False
    if not extracted.text.strip():
        return None, False
    query = _institution_key(institution)
    return source, any(
        _institution_key(cell) == query for row in extracted.text.splitlines() for cell in row.split(" | ")
    )


def _institution_key(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())
