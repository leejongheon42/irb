"""병원별로 모양이 다른 샘플 서식(DOCX) 생성기.

저장소에 바이너리를 넣지 않고 python-docx로 즉석 생성한다.
- A형(가나다대학교병원 스타일, ``{{ }}`` 문법): 심의의뢰서 — 체크박스, 연구진 행 반복, 머리글·바닥글
- B형(라마바의료원 스타일, ``[[ ]]``·``« »``·워드 편지병합 필드): 동의 면제 사유서
"""
from __future__ import annotations

from io import BytesIO

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Role, User
from .models import DocumentTemplate


def _title(doc, text: str, size: int = 18) -> None:
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(text)
    run.bold = True
    run.font.size = Pt(size)


def _kv_table(doc, rows: list[tuple[str, str]]) -> None:
    table = doc.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for key, value in rows:
        cells = table.add_row().cells
        cells[0].text = key
        cells[0].paragraphs[0].runs[0].bold = True
        cells[1].text = value


def _merge_field(paragraph, name: str) -> None:
    """워드 편지 병합 필드(MERGEFIELD)를 문단에 추가한다."""
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), f" MERGEFIELD {name} \\* MERGEFORMAT ")
    run = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = f"«{name}»"
    run.append(t)
    fld.append(run)
    paragraph._p.append(fld)


def sample_application_docx() -> bytes:
    """A형: 가나다대학교병원 심의의뢰서."""
    doc = Document()
    section = doc.sections[0]
    section.header.paragraphs[0].text = "«기관명» 기관생명윤리위원회 · 서식 제1호"
    section.footer.paragraphs[0].text = "[[위원회명]] · 문의 [[사무국연락처]]"
    _title(doc, "심 의 의 뢰 서")
    doc.add_paragraph("접수번호: {{과제번호}}    판: v{{버전}}")
    _kv_table(doc, [
        ("연구제목", "{{연구제목}}"),
        ("연구책임자", "{{연구책임자}} (소속: {{소속}}, 연락처: {{연락처}})"),
        ("연구기간", "{{연구기간}}"),
        ("연구유형", "{{연구유형}}"),
        ("연구비 지원", "{{연구비}}"),
        ("자료 출처", "{{데이터출처~cdw}} CDW   {{데이터출처~emr}} EMR   {{데이터출처~pacs}} PACS   {{데이터출처~lis}} 검사결과"),
        ("자료수집기간", "{{자료수집기간}}"),
        ("예상 대상자 수", "{{예상대상자수}}"),
        ("개인정보 처리", "{{개인정보처리수준}}"),
        ("동의", "{{동의방식=obtain}} 서면동의 취득   {{동의방식=waiver}} 서면동의 면제 신청"),
        ("권고 심의유형", "{{권고심의유형}} (위험도 {{auto.risk_score}}점)"),
    ])
    doc.add_paragraph()
    doc.add_paragraph("1. 연구 목적").runs[0].bold = True
    doc.add_paragraph("{{연구목적}}")
    doc.add_paragraph("2. 연구 방법").runs[0].bold = True
    doc.add_paragraph("{{연구방법}}")
    doc.add_paragraph("3. 수집 항목").runs[0].bold = True
    doc.add_paragraph("{{#수집항목}}  {{no}}) {{item}}")
    doc.add_paragraph("4. 연구진").runs[0].bold = True
    team = doc.add_table(rows=1, cols=4)
    team.style = "Table Grid"
    for cell, text in zip(team.rows[0].cells, ["구분", "성명", "소속", "교육 이수 만료"]):
        cell.text = text
    row = team.add_row().cells
    row[0].text = "{{#연구진}}{{item.역할}}"
    row[1].text = "{{item.성명}}"
    row[2].text = "{{item.소속}}"
    row[3].text = "{{item.교육만료일|YYYY.MM.DD}}"
    doc.add_paragraph()
    p = doc.add_paragraph("위와 같이 심의를 의뢰합니다.    {{작성일|YYYY년 MM월 DD일}}")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p = doc.add_paragraph("연구책임자  {{연구책임자}}  (서명) {{서명}}")
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def sample_waiver_docx() -> bytes:
    """B형: 라마바의료원 동의 면제 사유서(대괄호·편지병합 문법)."""
    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "[[기관명]]"
    _title(doc, "연구대상자 서면동의 면제 사유서", 16)
    _kv_table(doc, [
        ("과제명", "[[과제명]]"),
        ("책임연구자", "[[책임연구자]] / [[연구책임자소속]]"),
        ("접수번호", "[[접수번호]]"),
        ("개인정보 처리 수준", "[[개인정보처리수준]] · 보관 장소: [[보관장소]]"),
    ])
    doc.add_paragraph()
    doc.add_paragraph("가. 동의 획득이 현실적으로 불가능하거나 연구 타당성에 심각한 영향을 미치는 사유").runs[0].bold = True
    doc.add_paragraph("[[waiver_impracticable_reasons|번호]]")
    doc.add_paragraph("«동의면제사유»")
    doc.add_paragraph("나. 동의 거부 추정 사유가 없고 위험이 극히 낮은 근거").runs[0].bold = True
    doc.add_paragraph("동의 거부 추정 사유 없음 확인: [[waiver_no_presumed_refusal|check]]")
    doc.add_paragraph("«최소위험근거»")
    p = doc.add_paragraph("신청일: ")
    _merge_field(p, "신청일")
    p = doc.add_paragraph("책임연구자 [[책임연구자]] (인)")
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


SAMPLES = [
    ("application", "가나다대학교병원 심의의뢰서(샘플)", "sample_application.docx", sample_application_docx,
     {"서명": "__blank__"}),
    ("waiver_request", "라마바의료원 동의면제 사유서(샘플)", "sample_waiver.docx", sample_waiver_docx, {}),
]


def create_samples(db: Session, user: User, *, activate: bool = False, ip: str = "") -> list[DocumentTemplate]:
    """샘플 서식 2종을 등록(자동 매핑)하고 필요하면 활성화한다."""
    from . import service

    created = []
    for doc_type, name, filename, builder, extra in SAMPLES:
        tpl = service.upload_template(db, user, filename=filename, raw=builder(), doc_type=doc_type, name=name,
                                      notes="데모용 샘플 서식", ip=ip)
        if extra:
            service.save_mapping(db, user, tpl, {**tpl.mapping, **extra}, ip=ip)
        if activate and not service.unmapped(tpl):
            service.activate(db, user, tpl, ip=ip)
        created.append(tpl)
    return created


def seed_doctemplates(db: Session) -> list[DocumentTemplate]:
    """데모 데이터: 등록된 서식이 없을 때만 샘플 2종을 등록·활성화(멱등)."""
    if db.scalars(select(DocumentTemplate.id)).first():
        return []
    user = db.scalars(select(User).where(User.role == Role.ADMIN).order_by(User.id)).first()
    if user is None:
        return []
    return create_samples(db, user, activate=True, ip="seed")
