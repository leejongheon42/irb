"""문서 텍스트 인식.

지원 형식
- PDF: pypdf 텍스트층 추출. 텍스트층이 없으면 스캔본 경고 → allow_ai_ocr 이면 Claude PDF OCR
- DOCX / HWPX: ZIP 안의 XML(w:p·hp:p 문단, tbl/tr/tc 표)을 문서 순서대로 읽음. 표의 행은 "항목 | 내용" 한 줄
- HWP(HWP5): OLE 컨테이너의 BodyText/Section* 레코드(PARA_TEXT) 해석. 배포용·암호 문서는 미리보기 텍스트
- XLSX: 공유 문자열·셀 값을 행 단위로
- HTML / TXT / CSV / MD: 태그 제거, 인코딩 자동 판별(UTF-8, CP949)
- 이미지(PNG·JPEG·GIF·WEBP): Claude OCR (allow_ai_ocr) - 그 외에는 경고
- DOC·RTF·ODT·PPTX 등: LibreOffice로 PDF 변환 후 추출
"""
from __future__ import annotations

import html
import io
import logging
import re
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

log = logging.getLogger(__name__)

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
IMAGE_TYPES = {"png": "image/png", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}

OCR_SYSTEM = (
    "당신은 문서 전사(OCR) 도구입니다. 주어진 문서의 모든 글자를 읽는 순서대로 원문 그대로 옮겨 적으십시오. "
    "제목과 번호 체계(1., 가., ① 등)는 그대로 유지하고, 표는 각 행을 '항목 | 내용' 형식의 한 줄로 적으십시오. "
    "요약·설명·추측을 덧붙이지 말고, 읽을 수 없는 부분은 [판독 불가]로 표시하십시오."
)


@dataclass
class ExtractedText:
    text: str
    pages: int | None
    method: str
    warnings: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return len(self.text.strip())

    def to_dict(self) -> dict:
        return {"text": self.text, "pages": self.pages, "method": self.method, "warnings": list(self.warnings)}


# ====================================================================== 형식 판별
def detect_format(filename: str, raw: bytes) -> str:
    ext = Path(filename).suffix.lower()
    head = raw[:16]
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(OLE_MAGIC):
        return "hwp" if ext == ".hwp" or _ole_is_hwp(raw) else "ole"
    if head.startswith(b"PK"):
        try:
            names = set(zipfile.ZipFile(io.BytesIO(raw)).namelist())
        except zipfile.BadZipFile:
            return "unknown"
        if "word/document.xml" in names:
            return "docx"
        if any(n.startswith("Contents/section") for n in names):
            return "hwpx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "ppt/presentation.xml" in names:
            return "pptx"
        if "content.xml" in names:
            return "odf"
        return "zip"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"{\\rtf"):
        return "rtf"
    lowered = raw.lstrip()[:20].lower()
    if ext in (".html", ".htm") or lowered.startswith((b"<!doctype html", b"<html")):
        return "html"
    if ext in (".txt", ".csv", ".tsv", ".md", ".text", ".log"):
        return "text"
    return "text" if _looks_text(raw) else "unknown"


def _looks_text(raw: bytes) -> bool:
    sample = raw[:4000]
    if not sample or b"\x00" in sample:
        return False
    for enc in ("utf-8", "cp949"):
        try:
            sample.decode(enc)
            return True
        except UnicodeDecodeError:
            continue
    return False


def _ole_is_hwp(raw: bytes) -> bool:
    try:
        import olefile

        ole = olefile.OleFileIO(io.BytesIO(raw))
        try:
            return ole.exists("FileHeader") and ole.openstream("FileHeader").read(17) == b"HWP Document File"
        finally:
            ole.close()
    except Exception:  # olefile 미설치 또는 손상
        return False


# ====================================================================== 진입점
def extract_text(filename: str, raw: bytes, *, allow_ai_ocr: bool = False) -> ExtractedText:
    fmt = detect_format(filename, raw)
    try:
        if fmt == "pdf":
            return _pdf(raw, allow_ai_ocr)
        if fmt in ("docx", "hwpx", "xlsx"):
            return _zip_xml(fmt, raw)
        if fmt == "hwp":
            return _hwp5(raw)
        if fmt == "html":
            return ExtractedText(html_to_text(_decode(raw)), None, "html")
        if fmt == "text":
            return ExtractedText(_decode(raw), None, "text")
        if fmt in IMAGE_TYPES:
            return _image(raw, IMAGE_TYPES[fmt], allow_ai_ocr)
        if fmt in ("ole", "rtf", "odf", "pptx"):
            return _via_office(filename, raw, fmt, allow_ai_ocr)
        return ExtractedText("", None, "unknown", [f"지원하지 않는 파일 형식입니다({Path(filename).suffix or '확장자 없음'})."])
    except Exception as e:  # 손상 파일 등 - 파이프라인은 계속 진행
        log.info("text extraction failed for %s: %s", filename, e)
        return ExtractedText("", None, f"{fmt}:error", [f"텍스트 추출 실패: {type(e).__name__}: {e}"])


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ====================================================================== PDF
def _pdf(raw: bytes, allow_ai_ocr: bool) -> ExtractedText:
    from pypdf import PdfReader

    warnings: list[str] = []
    reader = PdfReader(io.BytesIO(raw))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            return ExtractedText("", None, "pdf", ["암호가 걸린 PDF는 읽을 수 없습니다."])
    pages = len(reader.pages)
    texts = []
    for page in reader.pages:
        try:
            texts.append(page.extract_text() or "")
        except Exception as e:  # 일부 페이지 손상
            warnings.append(f"일부 페이지 텍스트 추출 실패: {type(e).__name__}")
            texts.append("")
    text = "\n\n".join(t.strip() for t in texts if t.strip())
    if len(text.strip()) < 30 * max(pages, 1):
        warnings.append("텍스트층이 거의 없는 스캔본 PDF로 보입니다.")
        if allow_ai_ocr:
            ocr = _ai_ocr([_ai().pdf_block(raw)], "PDF 문서", warnings)
            if ocr:
                return ExtractedText(ocr, pages, "ai_ocr", warnings)
        else:
            warnings.append("AI OCR이 꺼져 있어 스캔 이미지의 글자는 인식하지 못했습니다.")
    return ExtractedText(text, pages, "pdf", warnings)


def _ai():
    from ..ai import client

    return client


def _ai_ocr(blocks: list[dict], label: str, warnings: list[str]) -> str:
    client = _ai()
    try:
        return client.text_call(
            system=OCR_SYSTEM,
            content=[*blocks, {"type": "text", "text": f"위 {label}의 텍스트를 전사하십시오."}],
            effort="low",
            max_tokens=32000,
            purpose="문서 OCR",
        ).strip()
    except client.AIError as e:
        warnings.append(f"AI OCR 실패: {e}")
        return ""


def _image(raw: bytes, media_type: str, allow_ai_ocr: bool) -> ExtractedText:
    warnings: list[str] = []
    if not allow_ai_ocr:
        return ExtractedText("", 1, "image", ["이미지 문서는 AI OCR이 필요합니다(AI 비활성 또는 정책상 전송 불가)."])
    ocr = _ai_ocr([_ai().image_block(raw, media_type)], "이미지 문서", warnings)
    return ExtractedText(ocr, 1, "ai_ocr" if ocr else "image", warnings)


# ====================================================================== DOCX / HWPX / XLSX (ZIP + XML)
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _children_until(elem: ET.Element, names: set[str], stop: set[str]) -> Iterator[ET.Element]:
    """elem 아래에서 names 태그를 찾되 stop 태그(중첩 표 등)의 내부는 들어가지 않는다."""
    for child in elem:
        tag = _local(child.tag)
        if tag in names:
            yield child
        elif tag not in stop:
            yield from _children_until(child, names, stop)


def _para_text(p: ET.Element) -> str:
    parts: list[str] = []

    def walk(e: ET.Element) -> None:
        for child in e:
            tag = _local(child.tag)
            if tag == "tbl":
                continue
            if tag == "t":
                parts.append("".join(child.itertext()))
            elif tag == "tab":
                parts.append("\t")
            elif tag in ("br", "lineBreak", "cr"):
                parts.append(" ")
            else:
                walk(child)

    walk(p)
    return re.sub(r"[  ]+", " ", "".join(parts)).strip()


def _cell_text(tc: ET.Element) -> str:
    texts = [_para_text(p) for p in _children_until(tc, {"p"}, {"tbl"})]
    for nested in _children_until(tc, {"tbl"}, set()):
        texts.extend(_table_lines(nested))
    return " ".join(t for t in texts if t)


def _table_lines(tbl: ET.Element) -> list[str]:
    lines = []
    for tr in _children_until(tbl, {"tr"}, {"tbl"}):
        cells = [_cell_text(tc) for tc in _children_until(tr, {"tc"}, {"tbl", "tr"})]
        while cells and not cells[-1]:
            cells.pop()
        if any(cells):
            lines.append(" | ".join(cells))
    return lines


def _is_heading(p: ET.Element) -> bool:
    for e in p.iter():
        tag = _local(e.tag)
        if tag in ("pStyle",):
            val = next((v for k, v in e.attrib.items() if _local(k) == "val"), "")
            if re.search(r"(?i)heading|title|제목", val):
                return True
        if tag == "outlineLvl":
            return True
    return False


def xml_lines(xml_bytes: bytes, *, mark_headings: bool = False) -> list[str]:
    """WordprocessingML / HWPML 공통 문단·표 추출(지역 이름 p, t, tbl, tr, tc 기준)."""
    root = ET.fromstring(xml_bytes)
    lines: list[str] = []

    def emit(e: ET.Element) -> None:
        tag = _local(e.tag)
        if tag == "tbl":
            lines.extend(_table_lines(e))
            return
        if tag == "p":
            text = _para_text(e)
            if text:
                lines.append(("# " + text) if mark_headings and _is_heading(e) else text)
            for nested in _children_until(e, {"tbl"}, set()):
                lines.extend(_table_lines(nested))
            return
        for child in e:
            emit(child)

    emit(root)
    return lines


def _zip_xml(fmt: str, raw: bytes) -> ExtractedText:
    z = zipfile.ZipFile(io.BytesIO(raw))
    if fmt == "docx":
        lines = xml_lines(z.read("word/document.xml"), mark_headings=True)
        return ExtractedText("\n".join(lines), None, "docx")
    if fmt == "hwpx":
        sections = sorted(
            (n for n in z.namelist() if re.match(r"Contents/section\d+\.xml$", n)),
            key=lambda n: int(re.findall(r"\d+", n)[-1]),
        )
        lines = []
        for name in sections:
            lines.extend(xml_lines(z.read(name)))
        return ExtractedText("\n".join(lines), len(sections) or None, "hwpx")
    return _xlsx(z)


def _xlsx(z: zipfile.ZipFile) -> ExtractedText:
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root:
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    lines: list[str] = []
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    for name in sheets:
        root = ET.fromstring(z.read(name))
        for row in (e for e in root.iter() if _local(e.tag) == "row"):
            cells = []
            for c in (e for e in row if _local(e.tag) == "c"):
                ctype = c.attrib.get("t", "")
                v = next((e.text or "" for e in c if _local(e.tag) == "v"), "")
                if ctype == "s" and v.isdigit() and int(v) < len(shared):
                    cells.append(shared[int(v)])
                elif ctype == "inlineStr":
                    cells.append("".join(t.text or "" for t in c.iter() if _local(t.tag) == "t"))
                else:
                    cells.append(v)
            while cells and not cells[-1].strip():
                cells.pop()
            if any(x.strip() for x in cells):
                lines.append(" | ".join(cells))
    return ExtractedText("\n".join(lines), len(sheets) or None, "xlsx")


# ====================================================================== HWP 5.x (OLE)
HWPTAG_BEGIN = 0x10
HWPTAG_PARA_TEXT = HWPTAG_BEGIN + 51  # 67
# 1 WCHAR 크기의 문자 컨트롤
_CHAR_CONTROLS = {0, 10, 13, 24, 25, 26, 27, 28, 29, 30, 31}


def iter_hwp_records(data: bytes) -> Iterator[tuple[int, int, bytes]]:
    """HWP 레코드 스트림 → (태그 ID, 레벨, 페이로드). 헤더: 태그 10비트·레벨 10비트·크기 12비트(0xFFF면 확장 4바이트)."""
    pos, n = 0, len(data)
    while pos + 4 <= n:
        header = int.from_bytes(data[pos:pos + 4], "little")
        pos += 4
        tag, level, size = header & 0x3FF, (header >> 10) & 0x3FF, (header >> 20) & 0xFFF
        if size == 0xFFF:
            if pos + 4 > n:
                break
            size = int.from_bytes(data[pos:pos + 4], "little")
            pos += 4
        yield tag, level, data[pos:pos + size]
        pos += size


def decode_para_text(payload: bytes) -> str:
    """PARA_TEXT 페이로드(UTF-16LE + 제어문자) → 문자열. 인라인·확장 컨트롤은 8 WCHAR를 차지한다."""
    out: list[str] = []
    n = len(payload) // 2
    i = 0
    while i < n:
        code = int.from_bytes(payload[2 * i:2 * i + 2], "little")
        if code >= 32:
            if 0xD800 <= code <= 0xDBFF and i + 1 < n:
                low = int.from_bytes(payload[2 * i + 2:2 * i + 4], "little")
                if 0xDC00 <= low <= 0xDFFF:
                    out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                    i += 2
                    continue
            if not 0xD800 <= code <= 0xDFFF:
                out.append(chr(code))
            i += 1
            continue
        if code in _CHAR_CONTROLS:
            if code == 10:
                out.append("\n")
            elif code in (24, 30, 31):
                out.append(" ")
            i += 1
            continue
        if code == 9:
            out.append("\t")
        i += 8
    return "".join(out)


def _hwp5(raw: bytes) -> ExtractedText:
    import olefile

    ole = olefile.OleFileIO(io.BytesIO(raw))
    try:
        header = ole.openstream("FileHeader").read()
        if not header.startswith(b"HWP Document File"):
            return ExtractedText("", None, "hwp", ["HWP 문서 서명이 올바르지 않습니다."])
        flags = int.from_bytes(header[36:40], "little") if len(header) >= 40 else 0
        compressed, encrypted, distribution = bool(flags & 1), bool(flags & 2), bool(flags & 4)
        if encrypted or distribution:
            preview = ""
            if ole.exists("PrvText"):
                preview = ole.openstream("PrvText").read().decode("utf-16-le", errors="ignore")
            kind = "암호가 걸린" if encrypted else "배포용"
            return ExtractedText(preview.strip(), None, "hwp:preview",
                                 [f"{kind} HWP 문서라 본문을 읽을 수 없어 미리보기 텍스트(일부)만 추출했습니다."])
        sections = sorted(
            (e for e in ole.listdir() if len(e) == 2 and e[0] == "BodyText" and e[1].startswith("Section")),
            key=lambda e: int(e[1][7:] or 0),
        )
        paragraphs: list[str] = []
        for entry in sections:
            data = ole.openstream(entry).read()
            if compressed:
                data = zlib.decompressobj(-15).decompress(data)
            for tag, _level, payload in iter_hwp_records(data):
                if tag == HWPTAG_PARA_TEXT:
                    text = decode_para_text(payload).strip()
                    if text:
                        paragraphs.append(text)
        return ExtractedText("\n".join(paragraphs), len(sections) or None, "hwp")
    finally:
        ole.close()


# ====================================================================== HTML / 기타
_BLOCK_END = re.compile(r"(?i)</(p|div|h[1-6]|li|tr|table|section|article|header|footer)>|<br\s*/?>")
_CELL_END = re.compile(r"(?i)</t[dh]>")


def html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", "", markup)
    markup = _CELL_END.sub(" | ", markup)
    markup = _BLOCK_END.sub("\n", markup)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", "", markup))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip(" |") for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _via_office(filename: str, raw: bytes, fmt: str, allow_ai_ocr: bool) -> ExtractedText:
    from .office import soffice_convert

    pdf = soffice_convert(filename, raw, "pdf")
    if pdf is None:
        return ExtractedText("", None, fmt, [f"{fmt.upper()} 형식은 LibreOffice가 있어야 읽을 수 있습니다(IRB_SOFFICE_PATH)."])
    result = _pdf(pdf, allow_ai_ocr)
    result.method = f"{fmt}→pdf"
    return result
