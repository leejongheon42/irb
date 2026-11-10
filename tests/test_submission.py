"""웹 문서 제출: 검증·종류 추정·버전·문서별 검토·미리보기·비교·합본·승인 도장·권한."""
from __future__ import annotations

import hashlib
import io
import json
import sys
import types
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from irb_automation.models import Document, Protocol, ProtocolStatus
from irb_automation.submission import service as sub
from irb_automation.submission.guess import guess_doc_type, guess_from_filename
from irb_automation.submission.models import DocumentComment
from irb_automation.submission.validate import safe_filename, validate_upload
from irb_automation.submission.versions import latest_by_type, register_new_version
from irb_automation.workflow import service as wf

from .conftest import csrf, login, make_pdf

S = ProtocolStatus


def sglt2(db) -> Protocol:
    return db.scalars(select(Protocol).where(Protocol.title.contains("SGLT2"))).one()


def encrypted_pdf() -> bytes:
    from pypdf import PdfReader, PdfWriter

    w = PdfWriter(clone_from=PdfReader(io.BytesIO(make_pdf())))
    w.encrypt(user_password="secret", owner_password="owner", algorithm="RC4-128")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def docx_bytes(text: str = "hello") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", f"<w:document><w:t>{text}</w:t></w:document>")
    return buf.getvalue()


# ------------------------------------------------------------------ 검증
def test_validate_accepts_real_pdf_with_page_count():
    r = validate_upload("protocol.pdf", make_pdf(3))
    assert r.ok and r.kind == "pdf" and r.page_count == 3 and r.mime_type == "application/pdf"


def test_validate_rejects_disguised_extension():
    r = validate_upload("동의서.pdf", b"MZ\x90\x00" + b"\x00" * 100)
    assert not r.ok and any("실행 파일" in e for e in r.errors)
    r = validate_upload("계획서.pdf", docx_bytes())
    assert not r.ok and r.kind == "docx" and any("확장자" in e for e in r.errors)
    r = validate_upload("scan.png", make_pdf())
    assert not r.ok and any("확장자" in e for e in r.errors)


def test_validate_rejects_empty_oversize_corrupt_and_encrypted():
    assert any("빈 파일" in e for e in validate_upload("a.pdf", b"").errors)
    assert any("초과" in e for e in validate_upload("a.pdf", make_pdf(), max_bytes=10).errors)
    assert any("손상" in e for e in validate_upload("a.pdf", b"%PDF-1.4 test").errors)
    enc = validate_upload("a.pdf", encrypted_pdf())
    assert enc.encrypted and any("비밀번호" in e for e in enc.errors)
    assert validate_upload("a.pdf", encrypted_pdf(), policy={"reject_encrypted_pdf": False}).ok
    assert not validate_upload("a.exe", b"MZ").ok
    assert not validate_upload("x.html", b"<html><script>alert(1)</script></html>").ok


def test_validate_detects_ooxml_and_macro_and_duplicate():
    r = validate_upload("계획서.docx", docx_bytes())
    assert r.ok and r.kind == "docx"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
        zf.writestr("word/vbaProject.bin", b"\x00")
    assert any("매크로" in e for e in validate_upload("m.docx", buf.getvalue()).errors)
    raw = make_pdf()
    dup = validate_upload("b.pdf", raw, existing=[(hashlib.sha256(raw).hexdigest(), "연구계획서 v1")])
    assert dup.ok and any("동일한 파일" in w for w in dup.warnings)


def test_safe_filename():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("..\\evil<>name.PDF") == "evil_name.pdf"
    assert safe_filename("연구 계획서(v2).pdf") == "연구_계획서_v2.pdf"


# ------------------------------------------------------------------ 종류 추정
@pytest.mark.parametrize("name,expected", [
    ("홍길동_이력서.pdf", "cv"),
    ("CV_Hong.pdf", "cv"),
    ("연구대상자 설명문 및 동의서_v2.hwp", "consent_form"),
    ("서면동의 면제 사유서.docx", "waiver_request"),
    ("CITI 이수증.pdf", "training_cert"),
    ("이해상충 공개서.pdf", "coi"),
    ("COI_form.pdf", "coi"),
    ("연구계획서 최종.hwpx", "protocol"),
    ("Protocol_v1.0.docx", "protocol"),
    ("증례기록서(CRF).xlsx", "crf"),
    ("가명처리 계획서.pdf", "deid_plan"),
    ("보안서약서.pdf", "security_pledge"),
    ("scan_0001.pdf", "other"),
])
def test_guess_from_filename(name, expected):
    assert guess_from_filename(name).doc_type == expected


def test_guess_uses_classifier_when_filename_unclear(monkeypatch):
    text_mod = types.ModuleType("irb_automation.docproc.text")
    text_mod.extract_text = lambda filename, raw, allow_ai_ocr=False: types.SimpleNamespace(text="연구대상자 동의 내용")
    cls_mod = types.ModuleType("irb_automation.docproc.classify")
    cls_mod.classify_document = lambda filename, text: ("consent_form", 0.8, "동의 문구")
    pkg = types.ModuleType("irb_automation.docproc")
    monkeypatch.setitem(sys.modules, "irb_automation.docproc", pkg)
    monkeypatch.setitem(sys.modules, "irb_automation.docproc.text", text_mod)
    monkeypatch.setitem(sys.modules, "irb_automation.docproc.classify", cls_mod)
    g = guess_doc_type("scan_0001.pdf", make_pdf())
    assert g.doc_type == "consent_form" and g.source == "classifier"
    assert guess_doc_type("이력서.pdf", make_pdf()).source == "filename"


# ------------------------------------------------------------------ 버전·검토
def test_new_upload_supersedes_previous_and_hook_called(db, users, monkeypatch):
    calls = []
    pipe = types.ModuleType("irb_automation.pipeline.service")
    pipe.start_document_pipeline = lambda db_, user, document_id: calls.append(document_id)
    monkeypatch.setitem(sys.modules, "irb_automation.pipeline", types.ModuleType("irb_automation.pipeline"))
    monkeypatch.setitem(sys.modules, "irb_automation.pipeline.service", pipe)

    p, r = sglt2(db), users["researcher"]
    d1, _ = sub.store_document(db, r, p, doc_type="protocol", filename="계획서.pdf", raw=make_pdf(1))
    d2, _ = sub.store_document(db, r, p, doc_type="protocol", filename="계획서_v2.pdf", raw=make_pdf(2))
    assert calls == [d1.id, d2.id]
    olds = [d for d in p.documents if d.doc_type == "protocol" and d.id != d2.id]
    assert olds and all(d.status == "superseded" and not d.is_current for d in olds)
    assert d2.is_current and d2.version == max(d.version for d in olds) + 1
    assert latest_by_type(list(p.documents))["protocol"] is d2

    # 자동 작성 문서 등 외부에서 만든 Document도 같은 규칙
    gen = Document(protocol_id=p.id, doc_type="protocol", filename="auto.html", stored_path="", sha256="0" * 64,
                   size=0, uploaded_by=r.id, source="generated")
    superseded = register_new_version(db, gen)
    assert superseded == [d2] and gen.version == d2.version + 1 and gen.is_current


def test_hook_failure_does_not_block_upload(db, users, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("pipeline down")

    pipe = types.ModuleType("irb_automation.pipeline.service")
    pipe.start_document_pipeline = boom
    monkeypatch.setitem(sys.modules, "irb_automation.pipeline", types.ModuleType("irb_automation.pipeline"))
    monkeypatch.setitem(sys.modules, "irb_automation.pipeline.service", pipe)
    p = sglt2(db)
    doc, _ = sub.store_document(db, users["researcher"], p, doc_type="cv", filename="cv.pdf", raw=make_pdf())
    db.commit()
    assert db.get(Document, doc.id) is not None


def test_team_comments_and_versions(db, users):
    p, r = sglt2(db), users["researcher"]
    doc, _ = sub.store_document(db, r, p, doc_type="consent_form", filename="동의서.pdf", raw=make_pdf())
    new, _ = sub.store_document(db, r, p, doc_type="consent_form", filename="동의서_v2.pdf", raw=make_pdf(2))
    assert new.version == doc.version + 1 and doc.status == "superseded" and new.is_current
    sub.add_comment(db, r, new, "2쪽 연락처 확인 필요")
    assert db.scalars(select(DocumentComment).where(DocumentComment.document_id == new.id)).all()
    with pytest.raises(wf.PermissionDenied):
        sub.add_comment(db, users["researcher2"].__class__(id=999999, username="x", full_name="x",
                                                            role=users["researcher"].role, password_hash=""), new, "x")


def test_upload_rules_by_status(db, users):
    p, r = sglt2(db), users["researcher"]
    p.status = S.SUBMITTED  # IRB 심의 중: IRB가 요청한 서류를 추가·교체할 수 있음
    db.flush()
    sub.store_document(db, r, p, doc_type="protocol", filename="p.pdf", raw=make_pdf())
    p.status = S.APPROVED
    db.flush()
    with pytest.raises(sub.SubmissionError, match="변경 준비"):
        sub.store_document(db, r, p, doc_type="protocol", filename="p2.pdf", raw=make_pdf(2))
    sub.store_document(db, r, p, doc_type="progress_report", filename="경과.pdf", raw=make_pdf())  # 보고 서류는 가능
    with pytest.raises(sub.SubmissionError, match="연구책임자"):
        sub.store_document(db, users["admin"], p, doc_type="coi", filename="c.pdf", raw=make_pdf())


# ------------------------------------------------------------------ HTTP
def test_http_bulk_upload_preview_compare_bundle(app, db):
    c = TestClient(app)
    login(c, "researcher")
    t = csrf(c)
    pid = sglt2(db).id
    files = [
        ("files", ("연구계획서.pdf", make_pdf(2, "line A\nline B"), "application/pdf")),
        ("files", ("홍길동_이력서.pdf", make_pdf(1), "application/pdf")),
        ("files", ("위장.pdf", b"MZ\x90\x00garbage", "application/pdf")),
        ("files", ("암호.pdf", encrypted_pdf(), "application/pdf")),
    ]
    r = c.post(f"/protocols/{pid}/documents/bulk", data={"csrf_token": t, "doc_type_0": "protocol", "doc_type_1": "auto"},
               files=files, follow_redirects=True)
    assert "성공 2건" in r.text and "거부 2건" in r.text
    db.expire_all()
    p = sglt2(db)
    cv = latest_by_type(list(p.documents))["cv"]
    assert cv.filename == "홍길동_이력서.pdf" and cv.page_count == 1
    assert "서류함" in c.get(f"/protocols/{pid}").text

    # 미리보기: 페이지 + iframe용 inline 응답 헤더
    proto = latest_by_type(list(p.documents))["protocol"]
    page = c.get(f"/documents/{proto.id}/preview")
    assert page.status_code == 200 and f"/documents/{proto.id}/inline" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    inline = c.get(f"/documents/{proto.id}/inline")
    assert inline.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in inline.headers["content-security-policy"]
    assert inline.headers["content-type"] == "application/pdf" and inline.headers["content-disposition"].startswith("inline")

    # 새 버전 업로드 후 비교
    t = csrf(c)
    c.post(f"/protocols/{pid}/documents", data={"csrf_token": t, "doc_type": "protocol"},
           files={"file": ("연구계획서_v2.pdf", make_pdf(2, "line A\nline C"), "application/pdf")})
    db.expire_all()
    new = latest_by_type(list(sglt2(db).documents))["protocol"]
    cmp = c.get(f"/documents/{new.id}/compare?with={proto.id}")
    assert cmp.status_code == 200 and "line C" in cmp.text and "diff-insert" in cmp.text and "diff-delete" in cmp.text

    # ZIP 합본: manifest 해시 = 실제 파일 해시
    z = c.get(f"/protocols/{pid}/bundle.zip")
    assert z.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["documents"] and "00_표지.html" in zf.namelist()
    for entry in manifest["documents"]:
        assert hashlib.sha256(zf.read(entry["file"])).hexdigest() == entry["sha256"]
        assert entry["integrity"] == "ok"
    assert any(e["doc_type"] == "protocol" and e["version"] == new.version for e in manifest["documents"])

    # PDF 합본: 표지 + 목차 + 각 PDF 쪽수
    from pypdf import PdfReader

    pdf = c.get(f"/protocols/{pid}/bundle.pdf")
    assert pdf.status_code == 200
    reader = PdfReader(io.BytesIO(pdf.content))
    from irb_automation.submission.bundle import bundle_documents

    db.expire_all()
    expected = 2 + sum(d.page_count or 0 for d in bundle_documents(sglt2(db)) if d.mime_type == "application/pdf")
    assert len(reader.pages) == expected


def test_http_document_permissions(app, db, users):
    p = sglt2(db)
    doc, _ = sub.store_document(db, users["researcher"], p, doc_type="consent_form", filename="동의서.pdf",
                                raw=make_pdf())
    db.commit()
    c = TestClient(app)
    login(c, "researcher")
    assert c.get(f"/documents/{doc.id}/preview").status_code == 200
    assert c.get("/documents/review-queue").status_code in (404, 422)  # IRB 측 검토 대기열 없음
    login(c, "admin")
    assert c.get(f"/documents/{doc.id}/preview").status_code == 200  # 관리자 열람


def test_seed_submission_is_idempotent(db, users):
    from irb_automation.submission.seed import seed_submission

    first = seed_submission(db)
    assert first > 0
    assert all(d.has_file for d in sglt2(db).documents)
    assert seed_submission(db) == 0
