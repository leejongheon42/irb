"""심의자료 합본.

- ZIP: 현행본 전체 + 표지(HTML) + manifest.json(파일명·종류·버전·SHA-256·업로드자·시각·무결성)
- PDF: 표지·목차 + PDF(또는 변환된 오피스·한글 문서) 병합, 문서별 책갈피. 병합할 수 없는 문서는 목차에 '별도 첨부'로 표기
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..engine.ruleset import get_ruleset
from ..models import Document, Protocol, User
from ..timeutil import now
from .pdfkit import korean_font
from .preview import pdf_bytes_for
from .validate import safe_filename
from .versions import current_documents

log = logging.getLogger(__name__)
TOC_ROWS_PER_PAGE = 26


def bundle_documents(protocol: Protocol) -> list[Document]:
    order = list(get_ruleset().catalog("document_types").keys())
    docs = [d for d in current_documents(list(protocol.documents)) if d.has_file]
    docs.sort(key=lambda d: (order.index(d.doc_type) if d.doc_type in order else len(order), d.doc_type))
    return docs


def _label(doc_type: str) -> str:
    return get_ruleset().label("document_types", doc_type)


def _entry_name(index: int, doc: Document) -> str:
    ext = Path(doc.filename).suffix.lower() or ".bin"
    return f"{index:02d}_{safe_filename(_label(doc.doc_type), 60).rsplit('.', 1)[0]}_v{doc.version}{ext}"


def manifest_for(protocol: Protocol, user: User, docs: list[Document]) -> dict[str, Any]:
    return {
        "protocol_no": protocol.protocol_no,
        "title": protocol.title,
        "pi": protocol.pi.full_name if protocol.pi else None,
        "status": protocol.status.label,
        "institution": get_settings().irb_name,
        "generated_at": now().isoformat(),
        "generated_by": user.full_name,
        "documents": [],
    }


def build_zip(protocol: Protocol, user: User) -> bytes:
    from ..web.deps import templates

    docs = bundle_documents(protocol)
    manifest = manifest_for(protocol, user, docs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, d in enumerate(docs, start=1):
            raw = Path(d.stored_path).read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            name = _entry_name(i, d)
            zf.writestr(name, raw)
            manifest["documents"].append({
                "file": name,
                "original_filename": d.filename,
                "doc_type": d.doc_type,
                "label": _label(d.doc_type),
                "version": d.version,
                "status": d.status_label,
                "source": d.source_label,
                "sha256": sha,
                "integrity": "ok" if sha == d.sha256 else "mismatch",
                "size": len(raw),
                "pages": d.page_count,
                "uploaded_by": d.uploader.full_name if d.uploader else None,
                "uploaded_at": d.uploaded_at.isoformat() if d.uploaded_at else None,
            })
        cover = templates.env.get_template("submission/bundle_cover.html").render(
            p=protocol, manifest=manifest, settings=get_settings())
        zf.writestr("00_표지.html", cover)
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    return buf.getvalue()


@dataclass
class _Item:
    doc: Document
    reader: Any
    pages: int
    note: str


def _collect(protocol: Protocol) -> list[_Item]:
    from pypdf import PdfReader

    items = []
    for d in bundle_documents(protocol):
        data = pdf_bytes_for(d)
        if data is None:
            items.append(_Item(d, None, 0, "별도 첨부(원본 형식)"))
            continue
        try:
            reader = PdfReader(io.BytesIO(data), strict=False)
            if reader.is_encrypted and not reader.decrypt(""):
                items.append(_Item(d, None, 0, "암호화 문서 - 별도 첨부"))
                continue
            items.append(_Item(d, reader, len(reader.pages), ""))
        except Exception:
            log.exception("bundle: unreadable pdf for document %s", d.id)
            items.append(_Item(d, None, 0, "읽을 수 없는 PDF - 별도 첨부"))
    return items


def _front_matter(protocol: Protocol, user: User, rows: list[dict[str, Any]], toc_pages: int) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    font, embedded = korean_font()

    def t(ko: str, en: str) -> str:
        return ko if embedded or ko.isascii() else en

    w, h = A4
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setTitle(f"{protocol.protocol_no or ''} IRB bundle")
    # 표지
    c.setFont(font, 22)
    c.drawCentredString(w / 2, h - 140, t("심의자료 합본", "IRB Review Package"))
    c.setFont(font, 12)
    c.drawCentredString(w / 2, h - 168, t(get_settings().irb_name, "Institutional Review Board"))
    y = h - 230
    fields = [
        (t("과제번호", "Protocol No."), protocol.protocol_no or "-"),
        (t("연구 제목", "Title"), t(protocol.title, protocol.protocol_no or "-")),
        (t("연구책임자", "PI"), t(protocol.pi.full_name if protocol.pi else "-", "-")),
        (t("상태", "Status"), t(protocol.status.label, protocol.status.value)),
        (t("수록 문서", "Documents"), f"{sum(1 for r in rows if r['page'])} / {len(rows)}"),
        (t("작성", "Generated"), f"{now():%Y-%m-%d %H:%M} · {t(user.full_name, user.username)}"),
    ]
    for label, value in fields:
        c.setFont(font, 11)
        c.drawString(80, y, label)
        lines = simpleSplit(str(value), font, 11, w - 260) or [""]
        for ln in lines:
            c.drawString(180, y, ln)
            y -= 18
        y -= 6
    c.setFont(font, 9)
    c.drawString(80, 80, t("각 문서의 SHA-256 해시는 ZIP 합본의 manifest.json에서 확인할 수 있습니다.",
                           "SHA-256 hashes of each document are listed in manifest.json of the ZIP package."))
    c.showPage()
    # 목차
    for page_no in range(toc_pages):
        c.setFont(font, 16)
        c.drawString(60, h - 70, t("목차", "Contents") + (f" ({page_no + 1}/{toc_pages})" if toc_pages > 1 else ""))
        c.setFont(font, 10)
        y = h - 110
        c.drawString(60, y, "No.")
        c.drawString(95, y, t("서류", "Document"))
        c.drawString(380, y, t("버전", "Ver."))
        c.drawString(430, y, t("쪽", "Page"))
        c.line(60, y - 6, w - 60, y - 6)
        y -= 26
        for r in rows[page_no * TOC_ROWS_PER_PAGE:(page_no + 1) * TOC_ROWS_PER_PAGE]:
            c.drawString(60, y, str(r["no"]))
            c.drawString(95, y, simpleSplit(t(r["label"], r["doc_type"]), font, 10, 270)[0])
            c.drawString(380, y, f"v{r['version']}")
            c.drawString(430, y, str(r["page"]) if r["page"] else t(r["note"], "attached separately"))
            y -= 22
        c.showPage()
    c.save()
    return buf.getvalue()


def build_pdf(protocol: Protocol, user: User) -> tuple[bytes, list[dict[str, Any]]]:
    from pypdf import PdfReader, PdfWriter

    items = _collect(protocol)
    toc_pages = max(1, math.ceil(len(items) / TOC_ROWS_PER_PAGE))
    page = 1 + toc_pages + 1
    rows = []
    for i, it in enumerate(items, start=1):
        row = {"no": i, "doc_type": it.doc.doc_type, "label": _label(it.doc.doc_type), "version": it.doc.version,
               "filename": it.doc.filename, "page": None, "pages": it.pages, "note": it.note}
        if it.reader is not None:
            row["page"] = page
            page += it.pages
        rows.append(row)
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(_front_matter(protocol, user, rows, toc_pages))))
    for it, row in zip(items, rows):
        if it.reader is None:
            continue
        start = len(writer.pages)
        writer.append(it.reader, import_outline=False)
        writer.add_outline_item(f"{row['no']:02d} {row['label']} v{row['version']}", start)
    writer.add_metadata({"/Title": f"{protocol.protocol_no or ''} 심의자료 합본", "/Creator": "IRB 자동화 솔루션"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), rows
