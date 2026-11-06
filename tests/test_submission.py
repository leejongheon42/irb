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

from irb_automation.models import DecisionType, Document, Protocol, ProtocolStatus
from irb_automation.submission import service as sub
from irb_automation.submission.guess import guess_doc_type, guess_from_filename
from irb_automation.submission.models import DocumentComment
from irb_automation.submission.stamp import stamp_approved_documents, stamp_pdf
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


def test_document_review_flow_and_permissions(db, users):
    p, r, coord, rev = sglt2(db), users["researcher"], users["coordinator"], users["reviewer1"]
    doc, _ = sub.store_document(db, r, p, doc_type="consent_form", filename="동의서.pdf", raw=make_pdf())
    with pytest.raises(wf.PermissionDenied):
        sub.review_document(db, r, doc, "accept")
    with pytest.raises(wf.PermissionDenied):
        sub.review_document(db, rev, doc, "accept")  # 배정되지 않은 위원
    with pytest.raises(wf.WorkflowError, match="사유"):
        sub.review_document(db, coord, doc, "request_revision", "")
    sub.review_document(db, coord, doc, "request_revision", "연락처 누락")
    assert doc.status == "revision_requested" and doc.reviewed_by == coord.id
    new, _ = sub.store_document(db, r, p, doc_type="consent_form", filename="동의서_보완.pdf", raw=make_pdf(2))
    assert new.status == "submitted" and doc.status == "superseded"
    thread = sub.comments_for(db, p, "consent_form")
    assert [c.kind for c in thread] == ["request_revision", "new_version"]
    with pytest.raises(wf.WorkflowError, match="이전 버전"):
        sub.review_document(db, coord, doc, "accept")
    sub.review_document(db, coord, new, "accept")
    assert new.status == "accepted"
    sub.add_comment(db, r, new, "감사합니다")
    assert db.scalars(select(DocumentComment).where(DocumentComment.document_id == new.id)).all()


def test_upload_restricted_during_review(db, users):
    p, r = sglt2(db), users["researcher"]
    p.status = S.UNDER_REVIEW
    db.flush()
    with pytest.raises(sub.SubmissionError, match="보완 요청된 서류만"):
        sub.store_document(db, r, p, doc_type="protocol", filename="p.pdf", raw=make_pdf())
    cur = latest_by_type(list(p.documents))["protocol"]
    cur.status = "revision_requested"
    db.flush()
    sub.store_document(db, r, p, doc_type="protocol", filename="p.pdf", raw=make_pdf())
    # 사무국은 언제나 가능
    sub.store_document(db, users["coordinator"], p, doc_type="coi", filename="c.pdf", raw=make_pdf())


# ------------------------------------------------------------------ 승인 도장
def test_stamp_pdf_adds_text():
    from pypdf import PdfReader

    out = stamp_pdf(make_pdf(2, "original body"), ["IRB APPROVED", "IRB-2026-0001", "2026-01-01 ~ 2027-01-01"])
    reader = PdfReader(io.BytesIO(out))
    assert len(reader.pages) == 2
    for page in reader.pages:
        text = page.extract_text()
        assert "IRB APPROVED" in text and "IRB-2026-0001" in text and "original body" in text


def test_approval_stamps_current_documents(db, users):
    p, r, coord = sglt2(db), users["researcher"], users["coordinator"]
    consent, _ = sub.store_document(db, r, p, doc_type="consent_form", filename="동의서.pdf", raw=make_pdf())
    sub.store_document(db, r, p, doc_type="protocol", filename="계획서.docx", raw=docx_bytes())  # 비PDF → 건너뜀
    wf.submit(db, r, p)
    d = wf.decide(db, coord, p, DecisionType.EXEMPT_CONFIRM)
    created = stamp_approved_documents(db, coord, p, d)
    assert [c.doc_type for c in created] == ["consent_form"]
    stamped = created[0]
    assert stamped.source == "stamped" and stamped.is_current and stamped.status == "accepted"
    assert consent.status == "superseded"  # 원본은 이전 버전으로 보존
    assert stamped.validation["stamp"]["source_document_id"] == consent.id
    from pypdf import PdfReader

    with open(stamped.stored_path, "rb") as f:
        text = PdfReader(f).pages[0].extract_text()
    assert "IRB EXEMPTION CONFIRMED" in text and p.protocol_no in text
    # 재실행 시 원본 기준으로 다시 찍는다(도장 중복 없음)
    again = stamp_approved_documents(db, coord, p, d)
    assert again[0].validation["stamp"]["source_document_id"] == consent.id


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
    assert "제출 서류 현황" in c.get(f"/protocols/{pid}").text

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


def test_http_review_queue_and_permissions(app, db, users):
    p = sglt2(db)
    doc, _ = sub.store_document(db, users["researcher"], p, doc_type="consent_form", filename="동의서.pdf",
                                raw=make_pdf())
    wf.submit(db, users["researcher"], p)
    db.commit()
    c = TestClient(app)
    login(c, "researcher")
    assert c.get("/documents/review-queue").status_code == 403
    t = csrf(c)
    assert c.post(f"/documents/{doc.id}/review", data={"csrf_token": t, "action": "accept"}).status_code == 403

    login(c, "coordinator")
    q = c.get("/documents/review-queue")
    assert q.status_code == 200 and "동의서.pdf" in q.text
    t = csrf(c)
    r = c.post(f"/documents/{doc.id}/review", data={"csrf_token": t, "action": "request_revision", "note": "서명란 누락"},
               follow_redirects=True)
    assert "보완 요청" in r.text and "서명란 누락" in r.text

    # 배정되지 않은 위원은 과제·합본 접근 불가
    login(c, "reviewer2")
    assert c.get(f"/documents/{doc.id}/preview").status_code == 403
    assert c.get(f"/protocols/{p.id}/bundle.zip").status_code == 403


def test_http_decide_creates_stamped_copy(app, db, users):
    p = sglt2(db)
    sub.store_document(db, users["researcher"], p, doc_type="consent_form", filename="동의서.pdf", raw=make_pdf())
    wf.submit(db, users["researcher"], p)
    db.commit()
    c = TestClient(app)
    login(c, "coordinator")
    t = csrf(c)
    r = c.post(f"/protocols/{p.id}/decide", data={"csrf_token": t, "decision": "exempt_confirm"}, follow_redirects=True)
    assert "승인 도장본 1건" in r.text
    db.expire_all()
    stamped = latest_by_type(list(sglt2(db).documents))["consent_form"]
    assert stamped.source == "stamped"


def test_seed_submission_is_idempotent(db, users):
    from irb_automation.submission.seed import seed_submission

    wf.submit(db, users["researcher"], sglt2(db))
    wf.decide(db, users["coordinator"], sglt2(db), DecisionType.EXEMPT_CONFIRM)
    first = seed_submission(db)
    assert first > 0
    stamped = [d for d in sglt2(db).documents if d.source == "stamped"]
    assert stamped
    assert seed_submission(db) == 0
    assert len([d for d in sglt2(db).documents if d.source == "stamped"]) == len(stamped)
