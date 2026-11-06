"""업로드 파일 검증.

- 확장자 화이트리스트와 실제 내용(매직바이트) 판별, 확장자-내용 불일치 거부(위장 파일 차단)
- 빈 파일·크기 제한, 실행 파일 차단
- PDF: 손상·비밀번호 암호화 감지, 쪽수, 스크립트/실행 동작 경고
- OOXML/HWPX: 필수 엔트리 확인, 매크로·압축 폭탄 의심 차단
- 같은 과제 내 동일 파일(SHA-256) 중복 경고, 저장용 파일명 정리
"""
from __future__ import annotations

import hashlib
import io
import re
import zipfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ALLOWED_EXT = {".pdf", ".doc", ".docx", ".hwp", ".hwpx", ".xls", ".xlsx", ".csv", ".txt", ".jpg", ".jpeg", ".png", ".zip"}

KIND_LABELS = {
    "pdf": "PDF 문서",
    "docx": "Word 문서(DOCX)",
    "xlsx": "Excel 문서(XLSX)",
    "hwpx": "한글 문서(HWPX)",
    "hwp": "한글 문서(HWP)",
    "doc": "Word 97-2003 문서(DOC)",
    "xls": "Excel 97-2003 문서(XLS)",
    "ole": "MS Office/한글 97 형식",
    "zip": "ZIP 압축 파일",
    "png": "PNG 이미지",
    "jpeg": "JPEG 이미지",
    "text": "텍스트",
    "exe": "실행 파일",
    "html": "HTML 문서",
    "empty": "빈 파일",
    "unknown": "알 수 없는 형식",
}

MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "hwpx": "application/hwp+zip",
    "hwp": "application/x-hwp",
    "doc": "application/msword",
    "xls": "application/vnd.ms-excel",
    "ole": "application/x-ole-storage",
    "zip": "application/zip",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "text": "text/plain",
}

# 확장자별 허용되는 실제 내용 종류
EXPECTED_KINDS: dict[str, set[str]] = {
    ".pdf": {"pdf"},
    ".docx": {"docx"},
    ".xlsx": {"xlsx"},
    ".hwpx": {"hwpx"},
    ".hwp": {"hwp", "ole"},
    ".doc": {"doc", "ole"},
    ".xls": {"xls", "ole"},
    ".zip": {"zip", "docx", "xlsx", "hwpx"},
    ".jpg": {"jpeg"},
    ".jpeg": {"jpeg"},
    ".png": {"png"},
    ".txt": {"text"},
    ".csv": {"text"},
}

OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
MAX_UNCOMPRESSED = 500 * 1024 * 1024  # 압축 해제 총량 상한(압축 폭탄 의심)
MAX_RATIO = 200


@dataclass
class ValidationResult:
    filename: str
    safe_name: str
    ext: str
    size: int
    sha256: str
    kind: str = "unknown"
    mime_type: str = "application/octet-stream"
    page_count: int | None = None
    encrypted: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def kind_label(self) -> str:
        return KIND_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["ok"] = self.ok
        out["kind_label"] = self.kind_label
        return out


def safe_filename(filename: str, max_len: int = 120) -> str:
    """저장용 파일명: 경로 제거, 허용 문자 외 '_' 치환, 선행 점 제거, 확장자 보존."""
    name = Path(filename.replace("\\", "/")).name
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    stem = re.sub(r"[^\w.\-가-힣]", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("._") or "file"
    ext = re.sub(r"[^\w.]", "", ext)[:10]
    return (stem[: max_len - len(ext)] + ext) or "file"


def _decodable_text(raw: bytes) -> bool:
    head = raw[:8192]
    if b"\x00" in head:
        return False
    for enc in ("utf-8-sig", "cp949"):
        try:
            head.decode(enc)
            return True
        except UnicodeDecodeError:
            continue
    # 잘린 멀티바이트 문자로 인한 실패를 고려해 끝 몇 바이트를 제외하고 재시도
    try:
        head[:-4].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def detect_kind(raw: bytes) -> tuple[str, dict[str, Any]]:
    """실제 내용으로 파일 형식 판별."""
    info: dict[str, Any] = {}
    if not raw:
        return "empty", info
    if raw[:2] == b"MZ" or raw[:4] == b"\x7fELF" or raw[:4] in (b"\xfe\xed\xfa\xce", b"\xcf\xfa\xed\xfe"):
        return "exe", info
    if b"%PDF-" in raw[:1024]:
        return "pdf", info
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", info
    if raw[:3] == b"\xff\xd8\xff":
        return "jpeg", info
    if raw[:8] == OLE_MAGIC:
        return _ole_kind(raw, info), info
    if raw[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        return _zip_kind(raw, info), info
    lowered = raw[:512].lstrip().lower()
    if lowered.startswith((b"<!doctype html", b"<html", b"<script", b"<svg")):
        return "html", info
    if _decodable_text(raw):
        return "text", info
    return "unknown", info


def _ole_kind(raw: bytes, info: dict[str, Any]) -> str:
    try:
        import olefile
    except ImportError:
        info["ole_detail"] = "olefile 미설치 - 세부 형식 미확인"
        return "ole"
    try:
        ole = olefile.OleFileIO(io.BytesIO(raw))
    except OSError:
        info["ole_error"] = "OLE 구조 손상"
        return "ole"
    try:
        streams = {"/".join(s) for s in ole.listdir(streams=True, storages=False)}
        info["ole_macro"] = any(s.lower().startswith(("macros", "_vba_project", "vba")) or "/vba/" in s.lower()
                                for s in streams)
        if ole.exists("FileHeader"):
            # HWP 5.0 FileHeader: 서명(32) + 버전(4) + 속성(4, bit0 압축 / bit1 암호)
            header = ole.openstream("FileHeader").read()
            if header.startswith(b"HWP Document File"):
                if len(header) >= 40:
                    flags = int.from_bytes(header[36:40], "little")
                    info["hwp_encrypted"] = bool(flags & 0x02)
                return "hwp"
        if ole.exists("WordDocument"):
            return "doc"
        if ole.exists("Workbook") or ole.exists("Book"):
            return "xls"
        return "ole"
    finally:
        ole.close()


def _zip_kind(raw: bytes, info: dict[str, Any]) -> str:
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        info["zip_error"] = "ZIP 구조 손상"
        return "zip"
    with zf:
        names = set(zf.namelist())
        total = sum(i.file_size for i in zf.infolist())
        compressed = sum(i.compress_size for i in zf.infolist()) or 1
        info["zip_entries"] = len(names)
        info["zip_uncompressed"] = total
        info["zip_bomb"] = total > MAX_UNCOMPRESSED or (total > 50 * 1024 * 1024 and total / compressed > MAX_RATIO)
        info["zip_encrypted"] = any(i.flag_bits & 0x1 for i in zf.infolist())
        info["ooxml_macro"] = any(n.lower().endswith("vbaproject.bin") for n in names)
        if "mimetype" in names:
            try:
                mt = zf.read("mimetype")[:64].decode("ascii", "ignore").strip()
            except (KeyError, zipfile.BadZipFile, RuntimeError):
                mt = ""
            if mt.startswith("application/hwp"):
                return "hwpx"
        if any(n.startswith("Contents/section") for n in names) and ("Contents/content.hpf" in names or "version.xml" in names):
            return "hwpx"
        if "[Content_Types].xml" in names:
            if "word/document.xml" in names:
                info["docx_pages"] = _ooxml_pages(zf, names)
                return "docx"
            if "xl/workbook.xml" in names:
                return "xlsx"
        return "zip"


def _ooxml_pages(zf: zipfile.ZipFile, names: set[str]) -> int | None:
    if "docProps/app.xml" not in names:
        return None
    try:
        m = re.search(rb"<Pages>(\d+)</Pages>", zf.read("docProps/app.xml"))
    except (KeyError, zipfile.BadZipFile, RuntimeError):
        return None
    return int(m.group(1)) if m else None


def _check_pdf(raw: bytes, result: ValidationResult, policy: dict[str, Any]) -> None:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError:
        result.warnings.append("PDF 세부 검사를 건너뛰었습니다(pypdf 미설치).")
        return
    if b"%%EOF" not in raw[-2048:]:
        result.warnings.append("PDF 끝 표시(%%EOF)가 없어 파일이 잘렸을 수 있습니다.")
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            result.encrypted = True
            try:
                opened = reader.decrypt("")
            except Exception:  # pypdf가 지원하지 않는 암호화 방식 등
                opened = 0
            if not opened:
                msg = "비밀번호로 보호된 PDF는 검토·도장·합본이 불가능합니다. 암호를 해제한 뒤 제출하십시오."
                if policy.get("reject_encrypted_pdf", True):
                    result.errors.append(msg)
                else:
                    result.warnings.append(msg)
                return
            result.warnings.append("권한 제한(보안 설정)이 걸린 PDF입니다. 승인 도장·합본이 제한될 수 있습니다.")
        result.page_count = len(reader.pages)
        if result.page_count == 0:
            result.errors.append("쪽이 없는 PDF입니다.")
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError, IndexError) as e:
        result.errors.append(f"손상된 PDF로 열 수 없습니다({type(e).__name__}).")
        return
    if policy.get("warn_pdf_active_content", True):
        active = [tag for tag in (b"/JavaScript", b"/JS ", b"/Launch", b"/EmbeddedFile", b"/OpenAction") if tag in raw]
        if active:
            result.warnings.append("PDF에 스크립트·실행 동작·내장 파일이 포함되어 있습니다: "
                                   + ", ".join(t.decode().strip("/ ") for t in active))


def validate_upload(
    filename: str,
    raw: bytes,
    *,
    max_bytes: int | None = None,
    existing: Iterable[tuple[str, str]] = (),
    policy: dict[str, Any] | None = None,
    allowed_ext: set[str] | None = None,
) -> ValidationResult:
    """업로드 파일 검증. existing=[(sha256, 표시명), ...] 은 같은 과제의 기존 문서."""
    policy = policy or {}
    allowed = allowed_ext or ALLOWED_EXT
    name = Path((filename or "").replace("\\", "/")).name
    ext = Path(name).suffix.lower()
    result = ValidationResult(
        filename=name, safe_name=safe_filename(name), ext=ext, size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    if not name:
        result.errors.append("파일 이름이 없습니다.")
    if ext not in allowed:
        result.errors.append(f"허용되지 않는 파일 형식입니다({ext or '확장자 없음'}). "
                             "PDF, HWP/HWPX, DOC/DOCX, XLS/XLSX, CSV/TXT, JPG/PNG, ZIP만 제출할 수 있습니다.")
    if not raw:
        result.kind = "empty"
        result.errors.append("빈 파일입니다.")
        return result
    if max_bytes is not None and len(raw) > max_bytes:
        result.errors.append(f"파일 크기({len(raw) / 1024 / 1024:.1f}MB)가 제한({max_bytes // 1024 // 1024}MB)을 초과했습니다.")

    kind, info = detect_kind(raw)
    result.kind = kind
    result.info.update(info)
    result.mime_type = MIME.get(kind, "application/octet-stream")

    if kind == "exe":
        result.errors.append("실행 파일은 제출할 수 없습니다.")
    elif kind == "html":
        result.errors.append("HTML/스크립트 파일은 제출할 수 없습니다.")
    elif ext in EXPECTED_KINDS and kind not in EXPECTED_KINDS[ext]:
        result.errors.append(f"확장자({ext})와 실제 파일 형식({KIND_LABELS.get(kind, kind)})이 다릅니다. "
                             "올바른 형식으로 저장한 파일을 제출하십시오.")

    if kind == "pdf" and not result.errors:
        _check_pdf(raw, result, policy)
    if kind in ("docx", "xlsx", "hwpx", "zip"):
        if info.get("zip_error"):
            result.errors.append("손상된 압축(OOXML/HWPX) 파일입니다.")
        if info.get("zip_bomb"):
            result.errors.append("압축 해제 용량이 비정상적으로 큰 파일입니다(압축 폭탄 의심).")
        if info.get("ooxml_macro"):
            result.errors.append("매크로가 포함된 문서는 제출할 수 없습니다(.docm/.xlsm 위장 의심).")
        if info.get("zip_encrypted"):
            result.warnings.append("암호화된 압축 항목이 있어 내용을 확인할 수 없습니다.")
        if kind == "docx" and info.get("docx_pages"):
            result.page_count = info["docx_pages"]
    if kind in ("hwp", "doc", "xls", "ole"):
        if info.get("ole_error"):
            result.errors.append("손상된 문서 파일입니다.")
        if info.get("hwp_encrypted"):
            result.errors.append("암호가 설정된 한글(HWP) 문서는 검토할 수 없습니다. 암호를 해제한 뒤 제출하십시오.")
        if info.get("ole_macro"):
            result.warnings.append("매크로(VBA)가 포함된 문서입니다. 매크로 없는 형식(PDF 권장)으로 제출하십시오.")

    dup = [label for sha, label in existing if sha == result.sha256]
    if dup:
        result.warnings.append("동일한 파일이 이미 제출되어 있습니다: " + ", ".join(dup[:3]))
    return result
