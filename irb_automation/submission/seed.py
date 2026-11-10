"""데모 데이터: 파일 없이 생성된 시드 문서에 합성 PDF를 만들고, 승인된 과제에 승인 도장본을 생성한다(멱등)."""
from __future__ import annotations

import hashlib
import io
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..engine.ruleset import get_ruleset
from ..models import Document
from .pdfkit import korean_font
from .validate import safe_filename, validate_upload


def _demo_pdf(doc: Document) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    font, embedded = korean_font()
    p = doc.protocol
    label = get_ruleset().label("document_types", doc.doc_type)
    w, h = A4
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    title = label if embedded else doc.doc_type
    c.setFont(font, 20)
    c.drawCentredString(w / 2, h - 110, title)
    c.setFont(font, 11)
    y = h - 160
    body = [
        f"{'과제' if embedded else 'Protocol'}: {p.title if embedded else (p.protocol_no or p.id)}",
        f"{'연구책임자' if embedded else 'PI'}: {p.pi.full_name if embedded and p.pi else '-'}",
        "※ 데모용 합성 문서입니다(실제 서류 아님)." if embedded else "Synthetic demo document.",
        "",
        (p.objectives or "") if embedded else "",
    ]
    for para in body:
        for ln in simpleSplit(para, font, 11, w - 140) or [""]:
            c.drawString(70, y, ln)
            y -= 18
    c.showPage()
    c.setFont(font, 11)
    c.drawString(70, h - 90, ("연구 방법 요약" if embedded else "Methods"))
    y = h - 120
    for ln in simpleSplit((p.methods or "-") if embedded else "-", font, 11, w - 140):
        c.drawString(70, y, ln)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


def seed_submission(db: Session) -> int:
    """파일이 없는 데모 문서에 PDF를 채운다. 생성한 파일 수를 반환."""
    settings = get_settings()
    created = 0
    for d in db.scalars(select(Document).where(Document.stored_path == "")):
        raw = _demo_pdf(d)
        folder = settings.upload_dir / str(d.protocol_id)
        folder.mkdir(parents=True, exist_ok=True)
        name = d.filename if d.filename.lower().endswith(".pdf") else f"{d.filename}.pdf"
        path = folder / f"{uuid.uuid4().hex}_{safe_filename(name)}"
        path.write_bytes(raw)
        result = validate_upload(name, raw)
        d.filename = name
        d.stored_path = str(path)
        d.sha256 = hashlib.sha256(raw).hexdigest()
        d.size = len(raw)
        d.mime_type = "application/pdf"
        d.page_count = result.page_count
        d.validation = result.to_dict()
        created += 1
    db.flush()
    db.flush()
    return created
