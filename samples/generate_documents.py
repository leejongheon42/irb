"""문서 자동화 데모·테스트용 '다른 병원 양식' 가상 연구계획서 생성기.

모든 기관명·인물·과제는 가상이며 실제 환자 정보를 포함하지 않는다.

  python samples/generate_documents.py [출력 디렉터리]

- hanbit_protocol.docx  : 표 + 번호 제목형(1. 가.) 양식 (DOCX)
- saesol_protocol.hwpx  : 로마숫자·원문자형(Ⅰ. ①) 양식 (HWPX)
- daon_protocol.hwp     : 한글 HWP 5.0 바이너리 양식
- hanbit_protocol.pdf   : 텍스트층이 있는 PDF
"""
from __future__ import annotations

import io
import struct
import sys
import zipfile
import zlib
from pathlib import Path

# ---------------------------------------------------------------------- 내용
HANBIT_TABLE = [
    ("연구과제명", "만성 신장병 환자에서 SGLT2 억제제 사용과 고칼륨혈증 발생의 연관성: 후향적 의무기록 연구"),
    ("연구책임자", "김연구 (신장내과)"),
    ("연구 기간", "IRB 승인일 ~ 2027년 12월 31일"),
    ("연구비 지원", "해당 없음"),
]
HANBIT_SECTIONS = [
    ("1. 연구 목적", "만성 신장병 3~4기 환자에서 SGLT2 억제제 사용이 고칼륨혈증 발생 위험에 미치는 영향을 DPP-4 억제제 사용군과 비교하여 평가한다."),
    ("2. 연구 배경 및 필요성", "SGLT2 억제제는 신장 보호 효과가 보고되었으나 진행된 만성 신장병 환자에서 전해질 이상에 대한 실제 진료 자료 근거는 부족하다."),
    ("3. 연구 대상", "2018년 1월부터 2023년 12월까지 본원 신장내과에서 SGLT2 억제제 또는 DPP-4 억제제를 처음 처방받은 성인 환자"),
    ("가. 선정 기준", "만 19세 이상, 만성 신장병(KCD N18.3, N18.4) 진단, 처방 전 6개월 이내 혈청 칼륨 측정 기록이 있는 환자"),
    ("나. 제외 기준", "투석 중이거나 신장이식을 받은 환자, 처방 전 고칼륨혈증 병력이 있는 환자"),
    ("4. 연구 방법", "임상데이터웨어하우스(CDW)와 전자의무기록(EMR)에서 처방·검사 자료를 추출하여 성향점수 매칭 후 Cox 비례위험모형으로 위험비를 추정하는 후향적 코호트 연구이다."),
    ("5. 목표 대상자 수 및 산출 근거", "목표 대상자 수: 약 1,200명 (연구 기간 내 적격 환자 전수, 사건 발생률 8% 가정 시 검정력 85%)"),
    ("6. 의무기록 열람 기간", "2018년 1월 1일 ~ 2023년 12월 31일"),
    ("7. 수집 항목", "연령, 성별, 혈청 칼륨, eGFR, 처방 약물, 동반질환 진단코드, 입원 여부"),
    ("8. 개인정보 보호 대책", "등록번호는 CDW팀이 연구용 일련번호로 가명처리하며 주민등록번호와 성명은 수집하지 않는다. 자료는 원내 분석 서버에서만 분석하고 연구자 계정별로 접근권한을 부여한다."),
    ("9. 연구대상자 동의", "후향적 연구로 대상자가 많고 상당수가 추적 불가하여 동의 획득이 현실적으로 불가능하므로 서면동의 면제를 신청한다."),
    ("10. 자료의 보관 및 폐기", "연구 종료 후 3년간 보관한 뒤 분석 서버에서 복구 불가능하게 영구 삭제한다."),
]

SAESOL_LINES = [
    "임상연구 심의 신청서 (새솔의료원 서식 제3호)",
    "Ⅰ. 연구 개요",
    "① 과제명: 소아 천식 환자의 흡입 스테로이드 순응도와 응급실 방문의 관련성",
    "② 연구 기간: 2026.03.01 ~ 2027.02.28",
    "Ⅱ. 연구의 목적 및 필요성",
    "소아 천식 환자에서 흡입 스테로이드 처방 순응도가 응급실 방문 빈도에 미치는 영향을 규명하여 교육 프로그램 개발의 근거를 마련한다.",
    "Ⅲ. 연구 방법",
    "본원 소아청소년과 천식 레지스트리와 전자의무기록을 이용한 후향적 관찰연구이며 보호자 설문은 시행하지 않는다.",
    "가. 연구대상자 선정 기준",
    "만 6세 이상 만 18세 미만 소아 천식(J45) 환자로 1년 이상 추적 관찰된 자",
    "나. 배제 기준",
    "선천성 심장질환 또는 낭포성 섬유증 동반 환자",
    "다. 예상 연구대상자 수 : 총 350명",
    "라. 자료 수집 기간 : 2019년 1월 ~ 2024년 12월",
    "마. 조사 항목",
    "연령, 성별, 진단명, 처방 내역, 응급실 방문 횟수, 보호자 연락처",
    "Ⅳ. 동의 취득",
    "보호자로부터 서면 동의서를 받고 만 7세 이상 아동에게는 별도 승낙을 받는다.",
    "Ⅴ. 개인정보 보호",
    "식별정보는 분석 전 연구용 번호로 대체하고 연구 종료 시 삭제한다.",
    "Ⅵ. 자료 보관",
    "연구 종료 후 5년간 잠금장치가 있는 서버에 보관한다.",
]
SAESOL_TABLE = [("구분", "내용"), ("연구책임자", "박소아 (소아청소년과)"), ("연구비", "새솔의료원 자체 연구비")]

DAON_LINES = [
    "연구계획서",
    "1. 연구제목: 흉부 CT 영상 딥러닝을 이용한 폐결절 악성도 예측 모델 개발",
    "2. 연구목적: 다온병원 PACS에 저장된 흉부 CT 영상으로 폐결절의 악성 여부를 예측하는 인공지능 모델을 개발하고 성능을 검증한다.",
    "3. 연구방법: 2015년부터 2022년까지 촬영된 흉부 CT DICOM 영상을 가명처리하여 합성곱 신경망을 학습시킨다.",
    "4. 연구대상자 수: 500명",
    "5. 자료수집기간: 2015-01-01 ~ 2022-12-31",
    "6. 동의: 과거 영상 자료를 이용하므로 동의 면제를 신청한다.",
]


# ---------------------------------------------------------------------- DOCX
def build_docx() -> bytes:
    import docx

    d = docx.Document()
    d.add_heading("임상연구 계획서 (한빛대학교병원 양식)", level=1)
    table = d.add_table(rows=0, cols=2)
    for k, v in HANBIT_TABLE:
        row = table.add_row().cells
        row[0].text, row[1].text = k, v
    for heading, body in HANBIT_SECTIONS:
        level = 3 if heading[:2] in ("가.", "나.") else 2
        d.add_heading(heading, level=level)
        d.add_paragraph(body)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------- HWPX
_HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
_HS = "http://www.hancom.co.kr/hwpml/2011/section"


def _hwpx_p(text: str) -> str:
    from xml.sax.saxutils import escape

    return f'<hp:p><hp:run><hp:t>{escape(text)}</hp:t></hp:run></hp:p>'


def build_hwpx() -> bytes:
    rows = "".join(
        f"<hp:tr><hp:tc><hp:subList>{_hwpx_p(a)}</hp:subList></hp:tc><hp:tc><hp:subList>{_hwpx_p(b)}</hp:subList></hp:tc></hp:tr>"
        for a, b in SAESOL_TABLE
    )
    table = f"<hp:p><hp:run><hp:tbl>{rows}</hp:tbl></hp:run></hp:p>"
    body = "".join(_hwpx_p(line) for line in SAESOL_LINES[:4]) + table + "".join(_hwpx_p(line) for line in SAESOL_LINES[4:])
    section = f'<?xml version="1.0" encoding="UTF-8"?><hs:sec xmlns:hs="{_HS}" xmlns:hp="{_HP}">{body}</hs:sec>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("mimetype", "application/hwp+zip")
        z.writestr("version.xml", '<?xml version="1.0" encoding="UTF-8"?><hv:HCFVersion xmlns:hv="http://www.hancom.co.kr/hwpml/2011/version"/>')
        z.writestr("Contents/content.hpf", '<?xml version="1.0" encoding="UTF-8"?><opf:package xmlns:opf="http://www.idpf.org/2007/opf/"/>')
        z.writestr("Contents/section0.xml", section)
    return buf.getvalue()


# ---------------------------------------------------------------------- HWP 5.0 (최소 OLE 복합 문서)
def _hwp_record(tag: int, payload: bytes, level: int = 0) -> bytes:
    size = len(payload)
    if size >= 0xFFF:
        return struct.pack("<II", tag | (level << 10) | (0xFFF << 20), size) + payload
    return struct.pack("<I", tag | (level << 10) | (size << 20)) + payload


def hwp_body(paragraphs: list[str]) -> bytes:
    """문단 목록 → BodyText 레코드 스트림(PARA_HEADER + PARA_TEXT)."""
    out = b""
    for p in paragraphs:
        out += _hwp_record(0x10 + 50, b"\x00" * 22)  # PARA_HEADER(내용은 파서가 사용하지 않음)
        out += _hwp_record(0x10 + 51, (p + "\r").encode("utf-16-le"), level=1)
    return out


def _cfb(file_header: bytes, section0: bytes) -> bytes:
    """FileHeader + BodyText/Section0 두 스트림만 가진 최소 Compound File(v3, 512바이트 섹터)."""
    sector = 512
    endchain, freesect, fatsect, nostream = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFD, 0xFFFFFFFF

    def pad(data: bytes, minimum: int = 4096) -> bytes:  # 4096 이상이면 미니 스트림을 쓰지 않는다
        data = data + b"\x00" * max(0, minimum - len(data))
        return data + b"\x00" * (-len(data) % sector)

    fh, sec = pad(file_header), pad(section0)
    fh_start = 2
    sec_start = fh_start + len(fh) // sector
    total = sec_start + len(sec) // sector
    if total > 128:
        raise ValueError("샘플 HWP가 너무 큽니다")
    fat = [freesect] * 128
    fat[0], fat[1] = fatsect, endchain
    for start, length in ((fh_start, len(fh) // sector), (sec_start, len(sec) // sector)):
        for i in range(length):
            fat[start + i] = start + i + 1 if i < length - 1 else endchain

    def entry(name: str, kind: int, left=nostream, right=nostream, child=nostream, start=endchain, size=0) -> bytes:
        raw_name = (name + "\x00").encode("utf-16-le") if name else b""
        return (raw_name.ljust(64, b"\x00") + struct.pack("<HBB", len(raw_name), kind, 1)
                + struct.pack("<III", left, right, child) + b"\x00" * 16 + b"\x00" * 4 + b"\x00" * 16
                + struct.pack("<IQ", start, size))

    directory = (
        entry("Root Entry", 5, child=1)
        + entry("FileHeader", 2, right=2, start=fh_start, size=len(fh))
        + entry("BodyText", 1, child=3)
        + entry("Section0", 2, start=sec_start, size=len(sec))
    )
    header = (
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16
        + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\x00" * 6
        + struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, endchain, 0, endchain, 0)
        + struct.pack("<109I", 0, *([freesect] * 108))
    )
    return header + struct.pack("<128I", *fat) + directory + fh + sec


def build_hwp5(paragraphs: list[str] | None = None, *, compressed: bool = True, distribution: bool = False) -> bytes:
    body = hwp_body(paragraphs or DAON_LINES)
    flags = (1 if compressed else 0) | (4 if distribution else 0)
    file_header = b"HWP Document File".ljust(32, b"\x00") + struct.pack("<II", 0x05000300, flags)
    file_header = file_header.ljust(256, b"\x00")
    if compressed:
        comp = zlib.compressobj(9, zlib.DEFLATED, -15)
        body = comp.compress(body) + comp.flush()
    return _cfb(file_header, body)


# ---------------------------------------------------------------------- PDF
def korean_font_name() -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    name = "HYSMyeongJo-Medium"
    if name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(name))
    return name


def build_pdf() -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    font = korean_font_name()
    y = 800
    lines = ["임상연구 계획서 (한빛대학교병원 양식)"] + [f"{k}: {v}" for k, v in HANBIT_TABLE]
    for heading, body in HANBIT_SECTIONS:
        lines += [heading, body]
    for line in lines:
        for chunk in [line[i:i + 42] for i in range(0, len(line), 42)] or [""]:
            if y < 60:
                c.showPage()
                y = 800
            c.setFont(font, 10)
            c.drawString(50, y, chunk)
            y -= 16
    c.save()
    return buf.getvalue()


def build_blank_pdf() -> bytes:
    """텍스트층이 없는(스캔본과 같은) PDF."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.rect(100, 100, 300, 300)
    c.showPage()
    c.save()
    return buf.getvalue()


SAMPLES = {
    "hanbit_protocol.docx": build_docx,
    "saesol_protocol.hwpx": build_hwpx,
    "daon_protocol.hwp": build_hwp5,
    "hanbit_protocol.pdf": build_pdf,
}


def main(out_dir: str = "samples/documents") -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, builder in SAMPLES.items():
        (out / name).write_bytes(builder())
        print(f"생성: {out / name}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "samples/documents")
