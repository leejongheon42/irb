"""Resolve findings against the uploaded source documents for the MVP scorecard."""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, TypedDict

from pypdfium2 import PdfiumError

from ..docproc.convert import OFFICE_FORMATS
from ..docproc.office import soffice_convert
from ..docproc.source_evidence import SourcePosition, highlighted_page, locate_source
from ..docproc.text import extract_text
from ..qc.models import QCRun


class EvidenceDocument(TypedDict):
    key: str
    name: str
    path: str
    fmt: str


class _CitationText(TypedDict):
    doc: str
    name: str
    quote: str
    state: Literal["located", "ambiguous", "unavailable", "unverified"]


class Citation(_CitationText, total=False):
    position: SourcePosition


def _layout_pdf(doc: EvidenceDocument, path: Path) -> bytes | None:
    if doc["fmt"] == "pdf":
        return path.read_bytes()
    if doc["fmt"] not in OFFICE_FORMATS - {"text", "html"}:
        return None
    cached = path.with_name(f"{path.name}.layout.pdf")
    if cached.is_file():
        return cached.read_bytes()
    pdf = soffice_convert(doc["name"], path.read_bytes(), timeout=25)
    if pdf:
        # Publish a complete snapshot so the scorecard and PNG use identical pagination.
        with NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(pdf)
                file.close()
                os.replace(temporary, cached)
            finally:
                temporary.unlink(missing_ok=True)
    return pdf


def findings_evidence(run: QCRun, only: int | None = None) -> dict[str, list[Citation]]:
    """Collect verified locations for the visible, scored and manual-check findings."""
    visible = set(run.counts.get("notes", [])) | {
        issue_id for area in run.counts.get("areas", {}).values() for issue_id in area["issue_ids"]
    }
    docs: dict[str, EvidenceDocument] = {doc["key"]: doc for doc in run.docs}
    output: dict[str, list[Citation]] = {}
    by_doc: dict[str, list[Citation]] = {}
    for issue in run.issues:
        issue_id = issue["id"]
        if issue_id not in visible or (only is not None and issue_id != only):
            continue
        citations: list[Citation] = []
        output[str(issue_id)] = citations
        locations = [*issue.get("locations", []), *issue.get("ai_evidence", [])]
        if not locations and issue.get("evidence"):
            locations = [{"doc": issue.get("doc"), "name": issue.get("doc_name"), "quote": issue["evidence"]}]
        seen: set[tuple[str, str]] = set()
        for item in locations:
            doc = docs.get(item.get("doc"))
            quote = item.get("quote", "").strip()
            key = (doc["key"] if doc else "", quote)
            if not quote or key in seen:
                continue
            seen.add(key)
            unverified = issue.get("confidence") == "ai_unverified" and not issue.get("locations")
            citation = Citation(doc=key[0], name=doc["name"] if doc else item.get("name") or "",
                                quote=quote, state="unverified" if unverified else "unavailable")
            citations.append(citation)
            if doc and doc.get("path") and not unverified:
                by_doc.setdefault(doc["key"], []).append(citation)

    for key, citations in by_doc.items():
        doc = docs[key]
        path = Path(doc["path"])
        quotes = {citation["quote"] for citation in citations}
        try:
            if doc["fmt"] == "text":
                matches = locate_source(quotes, text=extract_text(doc["name"], path.read_bytes()).text)
            else:
                matches = locate_source(quotes, pdf=_layout_pdf(doc, path))
        except (OSError, PdfiumError):
            continue
        for citation in citations:
            match = matches.get(citation["quote"])
            if match is None:
                continue
            if match["ambiguous"]:
                citation["state"] = "ambiguous"
            elif match["position"] is not None:
                citation["position"] = match["position"]
                citation["state"] = "located"
    return output


def evidence_image(run: QCRun, issue_id: int, index: int, *, preview: bool = False) -> bytes | None:
    if index < 0:
        return None
    locations = run.counts.get("source_locations")
    if locations is None:
        locations = findings_evidence(run, only=issue_id)
    citations = locations.get(str(issue_id), [])
    if index >= len(citations):
        return None
    citation = citations[index]
    position = citation.get("position") or {}
    doc = next((doc for doc in run.docs if doc["key"] == citation["doc"]), None)
    if not doc or not position.get("page") or not position.get("boxes"):
        return None
    try:
        pdf = _layout_pdf(doc, Path(doc["path"]))
        return highlighted_page(pdf, position, preview=preview) if pdf else None
    except (OSError, PdfiumError, IndexError):
        return None
