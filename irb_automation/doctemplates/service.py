"""기관 서식 등록·매핑·활성화·채우기 서비스."""
from __future__ import annotations

import hashlib
import uuid
import zipfile
from collections import OrderedDict
from io import BytesIO
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit
from ..config import get_settings
from ..engine.ruleset import get_ruleset
from ..models import Role, User
from ..workflow import service as wf
from . import docx_engine, hwpx_engine
from . import placeholders as PH
from .fields import available_fields, is_known_path
from .filling import FillResult as FillResult
from .filling import fill as fill
from .filling import plain_text as plain_text
from .mapping import suggest_all
from .models import DocumentTemplate

MAX_TEMPLATE_BYTES = 20 * 1024 * 1024
MAX_UNCOMPRESSED = 200 * 1024 * 1024
MAX_ENTRIES = 3000
OLE_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ENGINES = {"docx": docx_engine, "hwpx": hwpx_engine}


class TemplateError(wf.WorkflowError):
    """서식 파일·매핑 오류."""


def require_template_admin(user: User) -> None:
    if user.role != Role.ADMIN:
        raise wf.PermissionDenied("기관 서식 관리 권한이 없습니다(시스템관리자·IRB 간사).")


# ---------------------------------------------------------------- 형식 검증
def sniff_format(filename: str, raw: bytes) -> str:
    """'docx' | 'hwpx'. 지원하지 않는 형식이면 TemplateError."""
    ext = Path(filename).suffix.lower()
    if not raw:
        raise TemplateError("빈 파일입니다.")
    if len(raw) > MAX_TEMPLATE_BYTES:
        raise TemplateError(f"서식 파일은 {MAX_TEMPLATE_BYTES // 1024 // 1024}MB 이하만 등록할 수 있습니다.")
    if raw[:8] == OLE_SIGNATURE:
        if ext == ".hwp":
            raise TemplateError("HWP(한글 바이너리) 형식은 지원하지 않습니다. 한글에서 '다른 이름으로 저장 → "
                                "HWPX(*.hwpx)' 또는 DOCX로 저장해 올려 주십시오.")
        raise TemplateError("구형 Word(DOC) 형식은 지원하지 않습니다. DOCX로 저장해 올려 주십시오.")
    if raw[:4] != b"PK\x03\x04":
        raise TemplateError("DOCX 또는 HWPX 서식 파일이 아닙니다.")
    try:
        zf = zipfile.ZipFile(BytesIO(raw))
    except zipfile.BadZipFile as e:
        raise TemplateError("손상된 압축 형식의 파일입니다.") from e
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
            raise TemplateError("압축 해제 크기가 비정상적으로 큰 파일입니다.")
        names = set(zf.namelist())
        if "word/document.xml" in names and "[Content_Types].xml" in names:
            fmt = "docx"
        elif hwpx_engine.section_names(zf) and (
            "mimetype" not in names or zf.read("mimetype").strip().startswith(hwpx_engine.MIMETYPE)
        ):
            fmt = "hwpx"
        else:
            raise TemplateError("DOCX(워드) 또는 HWPX(한글) 서식만 등록할 수 있습니다.")
    if ext in (".docx", ".hwpx") and ext[1:] != fmt:
        raise TemplateError(f"파일 확장자({ext})와 실제 형식({fmt.upper()})이 다릅니다.")
    return fmt


# ---------------------------------------------------------------- 탐지
def summarize(found: list[PH.Placeholder]) -> list[dict[str, Any]]:
    """같은 이름의 자리표시자를 묶어 매핑 대상 목록을 만든다(반복 행 안의 item/no 제외)."""
    groups: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for ph in found:
        if ph.is_item_scoped or ph.marker == "/":
            continue
        g = groups.setdefault(ph.name, {"name": ph.name, "kind": ph.kind, "raw": [], "count": 0})
        g["count"] += 1
        if ph.kind == "list":
            g["kind"] = "list"
        elif ph.kind == "compare" and g["kind"] == "field":
            g["kind"] = "compare"
        if ph.raw not in g["raw"] and len(g["raw"]) < 5:
            g["raw"].append(ph.raw)
    return list(groups.values())


def detect(fmt: str, raw: bytes) -> list[dict[str, Any]]:
    try:
        return summarize(ENGINES[fmt].detect(raw))
    except (zipfile.BadZipFile, KeyError, ValueError) as e:
        raise TemplateError(f"서식 파일을 해석할 수 없습니다: {e}") from e
    except Exception as e:  # python-docx/lxml 파싱 오류 등
        raise TemplateError(f"서식 파일을 해석할 수 없습니다({type(e).__name__}).") from e


# ---------------------------------------------------------------- 등록·매핑
def _store(raw: bytes, fmt: str) -> str:
    folder = get_settings().data_dir / "doctemplates"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}.{fmt}"
    path.write_bytes(raw)
    return str(path)


def upload_template(
    db: Session, user: User, *, filename: str, raw: bytes, doc_type: str, name: str = "", notes: str = "",
    ip: str = "", source_id: int | None = None, variant: str = "",
) -> DocumentTemplate:
    require_template_admin(user)
    rs = get_ruleset()
    if doc_type not in rs.catalog("document_types"):
        raise TemplateError("서류 종류를 선택하십시오.")
    fmt = sniff_format(filename, raw)
    placeholders = detect(fmt, raw)
    source = None
    if source_id is not None:
        from ..docproc.text import extract_text
        from ..public_irb.source_models import SourceDocument
        from ..public_irb.sources import SourceError, source_bytes

        source = db.get(SourceDocument, source_id)
        expected = {
            ("protocol", "intervention"): "38", ("protocol", "survey"): "39",
            ("protocol", "specimen"): "40", ("protocol", "stem_cell"): "41", ("protocol", "records"): "43",
            ("consent_form", "human"): "44", ("consent_form", "specimen"): "45",
            ("assent_form", "human"): "46", ("assent_form", "specimen"): "47", ("final_report", ""): "57",
        }.get((doc_type, variant))
        if (source is None or not source.verified or source.owner != "researcher"
                or expected is None or (source.series, source.number) != ("권고서식", expected)):
            raise TemplateError("이 서식 종류와 연구 유형에 해당하는 검수 완료 원문을 선택하십시오.")
        try:
            original = extract_text(source.original_filename, source_bytes(source), allow_ai_ocr=False)
        except SourceError as exc:
            raise TemplateError("원문을 확인할 수 없습니다.") from exc
        if not original.text.strip() or original.warnings:
            raise TemplateError("원문 본문을 확인한 뒤 파생 서식을 등록하십시오.")
        edited = extract_text(filename, raw, allow_ai_ocr=False)
        def normalize(value: str) -> str:
            import re

            return re.sub(r"[\s|#]+", "", value)
        candidate = edited.text
        for item in placeholders:
            for marker in item["raw"]:
                candidate = candidate.replace(marker, "")
        if normalize(original.text) != normalize(candidate):
            raise TemplateError("파생본에 원문과 다른 본문이 있습니다. 자리표시자 외 문구를 대조하십시오.")
    version = 1 + (db.scalar(select(func.max(DocumentTemplate.version)).where(DocumentTemplate.doc_type == doc_type)) or 0)
    suggestions = suggest_all(placeholders, available_fields(db))
    tpl = DocumentTemplate(
        doc_type=doc_type,
        committee="public_irb" if source else "institution",
        variant=variant if source else "",
        source_document_id=source.id if source else None,
        derived_from_sha=source.sha256 if source else "",
        name=(name or "").strip() or f"{rs.label('document_types', doc_type)} 서식",
        fmt=fmt,
        original_filename=Path(filename).name,
        stored_path=_store(raw, fmt),
        sha256=hashlib.sha256(raw).hexdigest(),
        size=len(raw),
        version=version,
        active=False,
        placeholders=placeholders,
        mapping={k: v["path"] for k, v in suggestions.items()},
        notes=(notes or "").strip(),
        uploaded_by=user.id,
    )
    db.add(tpl)
    db.flush()
    audit.record(db, user, "template_upload", "doc_template", tpl.id,
                 {"doc_type": doc_type, "fmt": fmt, "version": version, "sha256": tpl.sha256,
                  "placeholders": len(placeholders), "auto_mapped": len(suggestions)}, ip)
    return tpl


def resolve_target(tpl: DocumentTemplate, name: str) -> tuple[str | None, str]:
    """자리표시자 이름 → (필드 경로 또는 None, 기본 형식)."""
    target = (tpl.mapping or {}).get(name, "")
    if not target and is_known_path(name):
        target = name
    if not target:
        return None, ""
    path, _, fmt = target.partition("|")
    return path.strip(), fmt.strip()


def unmapped(tpl: DocumentTemplate) -> list[str]:
    return [ph["name"] for ph in tpl.placeholders or [] if resolve_target(tpl, ph["name"])[0] is None]


def save_mapping(db: Session, user: User, tpl: DocumentTemplate, mapping: dict[str, str], ip: str = "") -> list[str]:
    require_template_admin(user)
    names = {ph["name"] for ph in tpl.placeholders or []}
    clean: dict[str, str] = {}
    invalid: list[str] = []
    for name, target in mapping.items():
        if name not in names:
            continue
        target = (target or "").strip()
        if not target:
            continue
        path = target.partition("|")[0].strip()
        if not is_known_path(path):
            invalid.append(name)
            continue
        clean[name] = target
    if invalid:
        raise TemplateError("알 수 없는 필드에 연결된 자리표시자: " + ", ".join(invalid))
    tpl.mapping = clean
    audit.record(db, user, "template_mapping", "doc_template", tpl.id,
                 {"mapped": len(clean), "unmapped": len(unmapped(tpl))}, ip)
    return unmapped(tpl)


def activate(db: Session, user: User, tpl: DocumentTemplate, ip: str = "") -> None:
    require_template_admin(user)
    if tpl.committee == "public_irb":
        from ..public_irb.source_models import SourceDocument

        src = db.get(SourceDocument, tpl.source_document_id) if tpl.source_document_id else None
        if not src or not src.verified or tpl.derived_from_sha != src.sha256:
            raise TemplateError("검수된 공용 IRB 원문을 연결해야 합니다.")
    missing = unmapped(tpl)
    if missing:
        raise TemplateError("매핑되지 않은 자리표시자가 있어 활성화할 수 없습니다: " + ", ".join(missing[:10])
                            + " — 필드를 연결하거나 '빈칸'/'그대로 둠'을 선택하십시오.")
    for other in db.scalars(select(DocumentTemplate).where(DocumentTemplate.doc_type == tpl.doc_type,
                                                           DocumentTemplate.committee == tpl.committee,
                                                           DocumentTemplate.variant == tpl.variant,
                                                           DocumentTemplate.active.is_(True))):
        other.active = False
    tpl.active = True
    db.flush()
    audit.record(db, user, "template_activate", "doc_template", tpl.id, {"doc_type": tpl.doc_type, "version": tpl.version}, ip)


def deactivate(db: Session, user: User, tpl: DocumentTemplate, ip: str = "") -> None:
    require_template_admin(user)
    tpl.active = False
    audit.record(db, user, "template_deactivate", "doc_template", tpl.id, {"doc_type": tpl.doc_type}, ip)


def active_template(db: Session, doc_type: str, committee: str = "institution", variant: str = "") -> DocumentTemplate | None:
    return db.scalars(
        select(DocumentTemplate).where(DocumentTemplate.doc_type == doc_type, DocumentTemplate.active.is_(True),
                                       DocumentTemplate.committee == committee, DocumentTemplate.variant == variant)
        .order_by(DocumentTemplate.version.desc())
    ).first()


def active_templates(db: Session) -> list[DocumentTemplate]:
    return list(db.scalars(select(DocumentTemplate).where(DocumentTemplate.active.is_(True))
                           .order_by(DocumentTemplate.doc_type)))


def versions(db: Session, doc_type: str) -> list[DocumentTemplate]:
    return list(db.scalars(select(DocumentTemplate).where(DocumentTemplate.doc_type == doc_type)
                           .order_by(DocumentTemplate.version.desc())))


def read_original(tpl: DocumentTemplate) -> bytes:
    path = Path(tpl.stored_path)
    if not path.is_file():
        raise TemplateError("서식 원본 파일을 찾을 수 없습니다.")
    return path.read_bytes()


# ---------------------------------------------------------------- PDF 변환(Fork D 연동)
def pdf_converter():
    """docproc.convert.to_pdf 가 있으면 반환."""
    try:
        from ..docproc.convert import to_pdf  # type: ignore[import-not-found]
    except ImportError:
        return None
    return to_pdf


def to_pdf(filename: str, content: bytes) -> bytes | None:
    converter = pdf_converter()
    if converter is None:
        return None
    try:
        return converter(filename, content)
    except Exception:  # 변환기 미설치·실패 시 원본 형식으로 제공
        return None
