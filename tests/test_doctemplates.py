"""기관 서식(DOCX/HWPX) 템플릿 엔진."""
from __future__ import annotations

import zipfile
from datetime import date
from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient
from sqlalchemy import select

from irb_automation.doctemplates import docx_engine, hwpx_engine, service
from irb_automation.doctemplates.fields import FieldResolver, available_fields, format_value
from irb_automation.doctemplates.mapping import suggest
from irb_automation.doctemplates.models import DocumentTemplate
from irb_automation.doctemplates.samples import sample_application_docx, sample_waiver_docx, seed_doctemplates
from irb_automation.documents import build_document, generatable_doc_types, generate_and_attach
from irb_automation.models import Protocol
from irb_automation.workflow import service as wf

from .conftest import csrf, login


def protocol(db) -> Protocol:
    return db.scalars(select(Protocol).where(Protocol.title.contains("SGLT2"))).one()


def docx_bytes(build) -> bytes:
    doc = Document()
    build(doc)
    out = BytesIO()
    doc.save(out)
    return out.getvalue()


def docx_text(raw: bytes) -> str:
    return docx_engine.plain_text(raw)


HP = 'xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph" xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"'


def make_hwpx(body: str) -> bytes:
    section = f'<?xml version="1.0" encoding="UTF-8" standalone="yes" ?><hs:sec {HP}>{body}</hs:sec>'
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/hwp+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("version.xml", '<?xml version="1.0"?><hv:HCFVersion xmlns:hv="x"/>')
        z.writestr("Contents/content.hpf", '<?xml version="1.0"?><opf:package xmlns:opf="x"/>')
        z.writestr("Contents/section0.xml", section)
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><c/>')
    return out.getvalue()


def p_(*runs: str) -> str:
    return "<hp:p>" + "".join(f'<hp:run charPrIDRef="0"><hp:t>{r}</hp:t></hp:run>' for r in runs) + "</hp:p>"


def upload(db, user, raw, doc_type="application", filename="t.docx"):
    return service.upload_template(db, user, filename=filename, raw=raw, doc_type=doc_type)


# ------------------------------------------------------------------ 엔진
def test_docx_split_runs_tables_headers(db, users):
    def build(doc):
        doc.sections[0].header.paragraphs[0].text = "{{기관명}}"
        p = doc.add_paragraph()
        p.add_run("제목: {{연구").bold = True
        p.add_run("제목}} / 일자 [[작성일|YYYY년 MM월 DD일]]")
        t = doc.add_table(rows=1, cols=2)
        t.rows[0].cells[0].text = "책임자"
        t.rows[0].cells[1].text = "«연구책임자» {{동의방식=waiver}}면제"
        doc.add_paragraph("{{objectives}}")

    tpl = upload(db, users["admin"], docx_bytes(build))
    names = {ph["name"] for ph in tpl.placeholders}
    assert {"기관명", "연구제목", "작성일", "연구책임자", "동의방식", "objectives"} <= names
    assert service.unmapped(tpl) == []  # 모두 자동 매핑
    result = service.fill(db, tpl, protocol(db))
    text = docx_text(result.content)
    assert "SGLT2" in text and "{{" not in text and "«" not in text
    assert f"{date.today().year}년" in text and "정연구 ■면제" in text
    doc = Document(BytesIO(result.content))
    first = doc.paragraphs[0].runs[0]
    assert first.bold and "SGLT2" in first.text  # 첫 run 서식 유지
    assert "○○대학교병원" in doc.sections[0].header.paragraphs[0].text
    assert result.filename == "심의의뢰서_v1.docx"


def test_docx_row_and_paragraph_repeat(db, users):
    def build(doc):
        doc.add_paragraph("{{#수집항목}}{{no}}. {{item}}")
        t = doc.add_table(rows=2, cols=2)
        t.rows[0].cells[0].text = "성명"
        t.rows[1].cells[0].text = "{{#연구진}}{{item.성명}}"
        t.rows[1].cells[1].text = "{{item.역할}}"

    tpl = upload(db, users["admin"], docx_bytes(build))
    out = Document(BytesIO(service.fill(db, tpl, protocol(db)).content))
    rows = out.tables[0].rows
    assert len(rows) == 3
    assert rows[1].cells[0].text == "정연구" and rows[1].cells[1].text == "연구책임자"
    assert rows[2].cells[0].text == "한공동"
    items = [p.text for p in out.paragraphs if p.text]
    assert items[0] == "1. 연구용 일련번호" and len(items) == 8


def test_docx_mergefield_unwrapped(db, users):
    raw = sample_waiver_docx()
    tpl = upload(db, users["admin"], raw, "waiver_request")
    assert "신청일" in {ph["name"] for ph in tpl.placeholders}
    assert tpl.mapping["동의면제사유"] == "waiver_impracticable_detail"
    text = docx_text(service.fill(db, tpl, protocol(db)).content)
    assert f"신청일: {date.today().isoformat()}" in text and "MERGEFIELD" not in text


def test_hwpx_fill_preserves_zip(db, users):
    body = (p_("과제: {{연구", "제목}}") + p_("[[작성일|한글]]")
            + '<hp:p><hp:run><hp:tbl><hp:tr><hp:tc><hp:subList>' + p_("{{#연구진}}{{item.성명}}")
            + "</hp:subList></hp:tc></hp:tr></hp:tbl></hp:run></hp:p>" + p_("{{methods}}"))
    raw = make_hwpx(body)
    assert service.sniff_format("a.hwpx", raw) == "hwpx"
    tpl = upload(db, users["admin"], raw, filename="a.hwpx")
    result = service.fill(db, tpl, protocol(db))
    assert result.filename.endswith(".hwpx")
    with zipfile.ZipFile(BytesIO(result.content)) as z:
        infos = z.infolist()
        assert infos[0].filename == "mimetype" and infos[0].compress_type == zipfile.ZIP_STORED
        assert {i.filename for i in infos} == {"mimetype", "version.xml", "Contents/content.hpf",
                                                "Contents/section0.xml", "META-INF/container.xml"}
    text = hwpx_engine.plain_text(result.content)
    assert "과제: 제2형 당뇨병" in text and "정연구" in text and "한공동" in text and "{{" not in text


def test_hwpx_newlines_become_linebreaks(db, users):
    raw = make_hwpx(p_("{{수집항목|줄바꿈}}"))
    tpl = upload(db, users["admin"], raw, filename="b.hwpx")
    with zipfile.ZipFile(BytesIO(service.fill(db, tpl, protocol(db)).content)) as z:
        xml = z.read("Contents/section0.xml").decode()
    assert xml.count("lineBreak") == 7


def test_format_value_rules():
    assert format_value(date(2026, 3, 5), "YYYY.MM.DD") == "2026.03.05"
    assert format_value(date(2026, 3, 5), "한글") == "2026년 03월 05일"
    assert format_value(True, "check") == "■" and format_value(False) == "아니오"
    assert format_value(["a", "b"], "번호") == "1. a\n2. b"
    assert format_value(8500) == "8,500" and format_value(8500, "raw") == "8500"


def test_suggest_aliases_and_similarity(db, users):
    fields = available_fields(db)
    assert suggest("과제명", "field", fields)["path"] == "title"
    assert suggest("연구 책임자 성명", "field", fields)["path"] == "pi_name"
    assert suggest("연구진", "list", fields)["path"] == "team"
    assert suggest("ㅁㄴㅇㄹ", "field", fields) is None


def test_resolver_custom_and_profile(db, users):
    p = protocol(db)
    r = FieldResolver(db, p)
    assert r.value("custom.anything") is None
    assert r.value("profile.institution_name")
    assert r.compare("data_sources", "=", "cdw") and not r.compare("data_sources", "=", "pacs")


# ------------------------------------------------------------------ 검증·활성화
def test_rejects_invalid_files(db, users):
    with pytest.raises(service.TemplateError, match="HWPX"):
        service.sniff_format("a.hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 100)
    with pytest.raises(service.TemplateError):
        service.sniff_format("a.docx", b"not a zip")
    with pytest.raises(service.TemplateError, match="확장자"):
        service.sniff_format("a.hwpx", sample_application_docx())
    with pytest.raises(wf.PermissionDenied):
        upload(db, users["researcher"], sample_application_docx())


def test_activation_requires_full_mapping_and_versions(db, users):
    coord = users["admin"]
    tpl = upload(db, coord, sample_application_docx())
    assert service.unmapped(tpl) == ["서명"]
    with pytest.raises(service.TemplateError, match="서명"):
        service.activate(db, coord, tpl)
    with pytest.raises(service.TemplateError, match="알 수 없는"):
        service.save_mapping(db, coord, tpl, {"서명": "no_such_field"})
    service.save_mapping(db, coord, tpl, {**tpl.mapping, "서명": "__keep__"})
    service.activate(db, coord, tpl)
    v2 = upload(db, coord, sample_application_docx())
    service.save_mapping(db, coord, v2, {**v2.mapping, "서명": "__blank__"})
    service.activate(db, coord, v2)
    assert v2.version == 2 and v2.active and not tpl.active
    assert service.active_template(db, "application").id == v2.id


def test_generate_and_attach_prefers_institution_template(db, users):
    p = protocol(db)
    assert generatable_doc_types(db)["application"] == "builtin"
    seed_doctemplates(db)
    assert seed_doctemplates(db) == []  # 멱등
    kinds = generatable_doc_types(db)
    assert kinds["application"] == "institution" and kinds["crf"] == "builtin"
    doc = generate_and_attach(db, users["researcher"], p, "application")
    assert doc.filename == "심의의뢰서_v1.docx" and doc.stored_path.endswith(".docx")
    assert build_document(db, p, "application", prefer_builtin=True).source == "builtin"
    # 기본 서식에 없는 종류도 기관 서식이 있으면 생성 가능
    tpl = upload(db, users["admin"], docx_bytes(lambda d: d.add_paragraph("{{연구제목}}")), "coi")
    service.activate(db, users["admin"], tpl)
    assert generatable_doc_types(db)["coi"] == "institution"
    assert "SGLT2" in docx_text(build_document(db, p, "coi").content)


def test_pdf_conversion_uses_docproc_when_available(monkeypatch):
    import sys
    import types

    mod = types.ModuleType("irb_automation.docproc.convert")
    mod.to_pdf = lambda filename, raw: b"%PDF-fake"
    pkg = types.ModuleType("irb_automation.docproc")
    monkeypatch.setitem(sys.modules, "irb_automation.docproc", pkg)
    monkeypatch.setitem(sys.modules, "irb_automation.docproc.convert", mod)
    assert service.to_pdf("a.docx", b"x") == b"%PDF-fake"


# ------------------------------------------------------------------ 화면
def test_admin_pages_and_permissions(app):
    with TestClient(app) as c:
        login(c, "researcher")
        assert c.get("/admin/templates").status_code == 403
        assert c.get("/doc-templates").status_code == 200
        login(c, "admin")
        t = csrf(c)
        r = c.post("/admin/templates", data={"csrf_token": t, "doc_type": "application", "name": "A"},
                   files={"file": ("x.hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 64, "application/octet-stream")},
                   follow_redirects=True)
        assert "HWPX" in r.text
        r = c.post("/admin/templates", data={"csrf_token": t, "doc_type": "application", "name": "A"},
                   files={"file": ("a.docx", sample_application_docx(), "application/octet-stream")},
                   follow_redirects=False)
        tid = int(r.headers["location"].rsplit("/", 1)[1])
        page = c.get(f"/admin/templates/{tid}")
        assert "미매핑" in page.text and "연구제목" in page.text
        from irb_automation.database import SessionLocal

        with SessionLocal() as db:
            tpl = db.get(DocumentTemplate, tid)
            form = {"csrf_token": t, "activate": "1"}
            for i, ph in enumerate(tpl.placeholders):
                form[f"ph_{i}"] = ph["name"]
                form[f"map_{i}"] = tpl.mapping.get(ph["name"], "__blank__")
        r = c.post(f"/admin/templates/{tid}/mapping", data=form, follow_redirects=True)
        assert "활성화했습니다" in r.text
        filled = c.get(f"/admin/templates/{tid}/fill?protocol_id=1")
        assert filled.status_code == 200 and filled.content[:2] == b"PK"
        assert c.get(f"/admin/templates/{tid}/file").status_code == 200
        # 연구자: 과제 화면 미리보기·첨부가 기관 서식을 사용
        login(c, "researcher")
        r = c.get("/protocols/1/generate/application")
        assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
        assert c.get("/protocols/1/generate/application?source=builtin").headers["content-type"].startswith("text/html")
        t = csrf(c)
        r = c.post("/protocols/1/generate/application", data={"csrf_token": t}, follow_redirects=True)
        assert "기관 서식으로" in r.text
        assert c.get(f"/doc-templates/{tid}/fill?protocol_id=1").status_code == 200
        login(c, "researcher2")
        assert c.get(f"/doc-templates/{tid}/fill?protocol_id=2").status_code == 403
