"""승인 도장(approval stamp).

승인·시정승인·심의면제 확인 시 지정된 서류(기본: 동의서·연구계획서)의 현행 PDF에
"IRB APPROVED / 과제번호 / 승인일 ~ 만료일" 도장을 찍은 사본을 만들어 새 현행본으로 등록한다.
원본은 이전 버전으로 보존되며, 이미 도장본이 현행본이면 원본을 기준으로 다시 찍는다(지속심의 기간 갱신 등).
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..models import Decision, DecisionType, Document, Protocol, User
from ..timeutil import now
from .pdfkit import korean_font
from .preview import pdf_bytes_for
from .service import policy, store_document
from .versions import latest_by_type

log = logging.getLogger(__name__)

STAMP_DECISIONS = {DecisionType.APPROVE, DecisionType.APPROVE_WITH_CONDITIONS, DecisionType.EXEMPT_CONFIRM}


class StampError(Exception):
    pass


def stamp_lines(protocol: Protocol, decision: Decision) -> list[str]:
    head = "IRB EXEMPTION CONFIRMED" if decision.decision == DecisionType.EXEMPT_CONFIRM else "IRB APPROVED"
    start = decision.approval_start or decision.decided_at.date()
    period = f"{start} ~ {decision.approval_end}" if decision.approval_end else f"{start}"
    return [head, protocol.protocol_no or "-", period, f"Ref. {decision.letter_no}"]


def _overlay(width: float, height: float, lines: list[str], korean_line: str | None) -> bytes:
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(width, height))
    rows = len(lines) + (1 if korean_line else 0)
    box_w, box_h = 200, 16 + 12 * rows
    x, y = width - box_w - 22, height - box_h - 22
    c.setStrokeColorRGB(0.72, 0.09, 0.09)
    c.setFillColorRGB(0.72, 0.09, 0.09)
    c.setLineWidth(1.6)
    c.roundRect(x, y, box_w, box_h, 6, stroke=1, fill=0)
    ty = y + box_h - 18
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(x + box_w / 2, ty, lines[0])
    c.setFont("Helvetica", 8.5)
    for ln in lines[1:]:
        ty -= 12
        c.drawCentredString(x + box_w / 2, ty, ln)
    if korean_line:
        font, _ = korean_font()
        ty -= 12
        c.setFont(font, 8)
        c.drawCentredString(x + box_w / 2, ty, korean_line)
    c.save()
    return buf.getvalue()


def stamp_pdf(raw: bytes, lines: list[str], *, korean_line: str | None = None, all_pages: bool = True) -> bytes:
    from pypdf import PdfReader, PdfWriter, Transformation

    reader = PdfReader(io.BytesIO(raw), strict=False)
    if reader.is_encrypted and not reader.decrypt(""):
        raise StampError("암호화된 PDF에는 도장을 찍을 수 없습니다.")
    writer = PdfWriter(clone_from=reader)
    for i, page in enumerate(writer.pages):
        if all_pages or i == 0:
            if page.rotation:
                page.transfer_rotation_to_content()
            box = page.mediabox
            overlay = PdfReader(io.BytesIO(_overlay(float(box.width), float(box.height), lines, korean_line))).pages[0]
            page.merge_transformed_page(overlay, Transformation().translate(float(box.left), float(box.bottom)))
    writer.add_metadata({"/Subject": " / ".join(lines), "/Creator": "IRB 자동화 솔루션(승인 도장)"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def stamp_approved_documents(db: Session, user: User, protocol: Protocol, decision: Decision,
                             ip: str = "") -> list[Document]:
    if decision.decision not in STAMP_DECISIONS:
        return []
    pol = policy()
    lines = stamp_lines(protocol, decision)
    _, embedded = korean_font()
    korean_line = get_settings().irb_name if embedded else None
    latest = latest_by_type(list(protocol.documents))
    created: list[Document] = []
    for doc_type in pol.get("stamp_doc_types") or []:
        current = latest.get(doc_type)
        if current is None:
            continue
        base = current
        if current.source == "stamped":
            src_id = (current.validation or {}).get("stamp", {}).get("source_document_id")
            src = db.get(Document, src_id) if src_id else None
            if src is None or not src.has_file:
                continue
            base = src
        data = pdf_bytes_for(base)
        if data is None:
            log.info("stamp skipped (not pdf/convertible): document %s", base.id)
            continue
        try:
            stamped = stamp_pdf(data, lines, korean_line=korean_line, all_pages=bool(pol.get("stamp_all_pages", True)))
        except Exception:
            log.exception("stamp failed for document %s", base.id)
            continue
        doc, _ = store_document(
            db, user, protocol, doc_type=doc_type, filename=f"{Path(base.filename).stem}_IRB승인본.pdf",
            raw=stamped, source="stamped", ip=ip, run_hook=False, check_permission=False, status="accepted",
        )
        doc.reviewed_by = user.id
        doc.reviewed_at = now()
        doc.review_note = f"승인 도장본 자동 생성({decision.letter_no})"
        doc.validation = {**(doc.validation or {}), "stamp": {
            "decision_id": decision.id, "letter_no": decision.letter_no, "source_document_id": base.id,
            "lines": lines,
        }}
        audit.record(db, user, "document_stamp", "protocol", protocol.id,
                     {"doc_id": doc.id, "source_doc_id": base.id, "doc_type": doc_type, "letter_no": decision.letter_no}, ip)
        created.append(doc)
        latest[doc_type] = doc
    return created


def stamp_after_decision(db: Session, user: User, protocol: Protocol, decision: Decision, ip: str = "") -> list[Document]:
    """결정 직후 호출. 도장 생성이 실패해도 결정은 유지된다(SAVEPOINT로 격리)."""
    try:
        with db.begin_nested():
            return stamp_approved_documents(db, user, protocol, decision, ip)
    except Exception:
        log.exception("approval stamping failed for protocol %s", protocol.id)
        return []
