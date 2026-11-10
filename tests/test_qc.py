"""서류 품질 점검 엔진·자동 교정·패키지 자동 완성."""
from __future__ import annotations

import sys
import types
from datetime import date

from sqlalchemy import select

from irb_automation.models import Protocol
from irb_automation.qc import autofix, checks, facts, samples
from irb_automation.qc import service as qcs


def _docs():
    return [qcs._read_doc(f"d{i}", n, f(), t) for i, (n, t, f) in enumerate(samples.SAMPLES, start=1)]


def test_version_from_filename_and_body():
    assert facts.version_from_filename("ICF_v3.0_최종.docx") == "3"
    assert facts.version_from_filename("계획서 Ver 2.1.docx") == "2.1"
    assert facts.version_from_filename("동의서.docx") is None
    vals = {f.value for f in facts.versions_in_body("표지\nVersion 2.0\n버전 2.0")}
    assert vals == {"2"}


def test_fact_extraction():
    text = ("시험약: Mira-001\n투여: 1일 1회 100mg 경구 투여\n시험 기간은 총 12주이며, 총 8회 방문한다.\n"
            "목표 연구대상자 수는 총 1,200명\n연구 기간: 2026.12.01 ~ 2027.11.30\n자료는 3년간 보관")
    got = {(f.key, f.value) for f in facts.extract(text)}
    assert {("dosage", "1회/100mg"), ("visits", "8"), ("duration", "12주"), ("subjects", "1200"),
            ("study_period", "2026-12-01~2027-11-30"), ("retention", "3"), ("drug", "mira-001")} <= got


def test_restyle_keeps_format():
    ref = facts.Fact("study_period", "2026-12-01~2027-11-30", "")
    assert facts.restyle("study_period", "2026년 1월 5일 ~ 2026년 6월 30일", ref) == "2026년 12월 1일 ~ 2027년 11월 30일"
    assert facts.restyle("subjects", "총 1,500명", facts.Fact("subjects", "1800", "")) == "총 1,800명"


def test_sample_rules_find_catcher_scenario():
    docs = _docs()
    p1, p3 = checks.run_rules(docs, None)
    issues = p1 + p3
    titles = " | ".join(i["title"] for i in issues)
    assert "투여 용량·횟수 불일치" in titles and "방문 횟수 불일치" in titles
    assert "파일명과 본문의 버전" in titles
    assert "'피험자' → '연구대상자'" in titles and "'부작용' → '이상반응'" in titles
    assert "자발적 참여 및 언제든 철회할 권리" in titles and "'연구대상지'" in titles
    score, verdict, counts = qcs.score(issues)
    assert verdict == "FAIL" and counts["critical"] >= 3 and score < 80
    # 계획서는 기준 문서이므로 교차 대조 이슈는 동의서에만
    assert all(i["doc"] == "d2" for i in issues if i["category"] == "cross")


def test_score_rules():
    mk = lambda sev: {"severity": sev}  # noqa: E731
    assert qcs.score([mk("major")] * 2) == (86, "PASS", {"critical": 0, "major": 2, "minor": 0})
    assert qcs.score([mk("critical")])[1] == "FAIL"  # Critical 1건이면 점수와 무관하게 FAIL
    assert qcs.score([mk("major")] * 3)[1] == "FAIL"  # 79점 < 80


def test_record_cross_check_against_protocol(db, users):
    p = Protocol(title="소아 연구", pi_id=users["researcher"].id, expected_subjects=1800,
                 study_start=date(2026, 12, 1), study_end=date(2027, 11, 30), retention_years=3)
    db.add(p)
    db.flush()
    doc = checks.QCDoc("d1", "동의서.txt", "consent_form",
                       "이 연구에는 총 1,500명의 연구대상자가 참여합니다.\n연구 기간: 2026.12.01 ~ 2027.06.30\n자료는 1년간 보관합니다.")
    p1, _ = checks.run_rules([doc], p)
    rec = {i["code"]: i for i in p1 if i["category"] == "record"}
    assert rec["RECORD_SUBJECTS"]["severity"] == "critical"
    assert "1,800명" in rec["RECORD_SUBJECTS"]["fixes"][0]["new"]
    assert rec["RECORD_STUDY_PERIOD"]["fixes"][0]["new"] == "2026.12.01 ~ 2027.11.30"
    assert any(i["code"] == "RETENTION_SHORT" for i in p1)


def test_autofix_docx_tracked_and_clean_preserve_layout():
    from io import BytesIO

    from docx import Document

    docs = _docs()
    p1, p3 = checks.run_rules(docs, None)
    fixes = [f for i in p1 + p3 for f in i["fixes"] if f["doc"] == "d2"]
    raw = samples.icf_docx()
    clean, rep, _ = autofix.apply("ICF_v3.0.docx", raw, fixes, mode="clean", font="맑은 고딕")
    tracked, _, _ = autofix.apply("ICF_v3.0.docx", raw, fixes, mode="tracked")
    assert rep.total >= 6
    text = qcs._read_doc("x", "ICF_v3.0.docx", clean, "consent_form").text
    assert "1일 1회 100mg" in text and "총 8회 방문" in text and "Version 3.0" in text
    assert "피험자" not in text and "부작용" not in text and "연구대상지" not in text
    d = Document(BytesIO(clean))
    assert "Version 3.0" in d.sections[0].header.paragraphs[0].text  # 머리글도 교정
    assert any(r.font.highlight_color is not None for p in d.paragraphs for r in p.runs)
    assert all(r.font.name == "맑은 고딕" for p in d.paragraphs for r in p.runs if r.text)
    import zipfile

    xml = zipfile.ZipFile(BytesIO(tracked)).read("word/document.xml").decode("utf-8")
    assert "<w:del " in xml and "<w:ins " in xml and "w:delText" in xml  # Word 변경 내용 추적
    # 재점검하면 교차 대조·버전 이슈가 사라진다
    again = [docs[0], qcs._read_doc("d2", "ICF_v3.0.docx", clean, "consent_form")]
    p1b, p3b = checks.run_rules(again, None)
    assert not [i for i in p1b if i["category"] in ("cross", "version", "terminology")]
    assert not [i for i in p3b if i["category"] == "typo"]


def test_autofix_rejects_pdf():
    from tests.conftest import make_pdf

    try:
        autofix.apply("a.pdf", make_pdf(), [{"old": "a", "new": "b"}])
    except autofix.AutofixError as e:
        assert "DOCX" in str(e)
    else:  # pragma: no cover
        raise AssertionError("PDF should not be auto-fixed")


def test_run_uploads_and_apply_fixes(db, users):
    files = [(n, f(), t) for n, t, f in samples.SAMPLES]
    qr = qcs.run_uploads(db, users["researcher"], files, use_ai=False)
    assert qr.verdict == "FAIL" and qr.phases["2"]["status"] == "skipped"
    ids = [i["id"] for i in qcs.fixable_issues(qr)]
    outs = qcs.apply_fixes(db, users["researcher"], qr, ids, font="맑은 고딕")
    assert outs and all(o.get("clean") for o in outs if not o.get("error"))
    assert any(o.get("tracked") for o in outs)


def test_ai_phase_with_fake_client(db, users, monkeypatch):
    calls = []
    fake = types.ModuleType("irb_automation.ai.client")

    class AIError(Exception):
        pass

    fake.AIError = AIError
    fake.ai_enabled = lambda: True
    fake.text_document_block = lambda text, title, **kw: {"type": "document", "title": title, "text": text}

    def structured_call(**kw):
        calls.append(kw)
        return {"summary": "동의서의 위험 설명이 계획서보다 축소됨", "issues": [{
            "doc_index": 1, "severity": "major", "category": "context", "title": "위험 설명 축소",
            "detail": "계획서에는 저혈당 위험이 있으나 동의서에 없음", "evidence": "예상되는 부작용으로는 두통",
            "suggestion": "저혈당 위험을 추가", "fix_old": "두통, 오심 등이", "fix_new": "두통, 오심, 저혈당 등이"}]}

    fake.structured_call = structured_call
    monkeypatch.setitem(sys.modules, "irb_automation.ai.client", fake)
    import irb_automation.ai as ai_pkg

    monkeypatch.setattr(ai_pkg, "client", fake, raising=False)
    qr = qcs.run(db, users["researcher"], _docs(), use_ai=True)
    ai_issues = [i for i in qr.issues if i["source"] == "ai"]
    assert ai_issues and ai_issues[0]["fixes"] == [{"doc": "d2", "old": "두통, 오심 등이", "new": "두통, 오심, 저혈당 등이"}]
    assert qr.ai_used and qr.phases["2"]["status"] == "done"
    assert "이미 찾은 이슈" in calls[0]["content"][-1]["text"]


def test_package_autocomplete(db, users):
    from irb_automation.package import service as pkg

    p = db.scalars(select(Protocol).where(Protocol.title.contains("자살"))).one()
    before = pkg.plan(db, p)
    assert before["done"] == 0 and before["can_generate"]
    out = pkg.autocomplete(db, users["researcher"], p, use_ai=False)
    assert out["generated"] and out["qc_run"] is not None
    assert "access_controls" in out["drafted"] and p.access_controls
    after = pkg.plan(db, p)
    assert after["done"] > before["done"]
    data, name = pkg.build_zip(db, users["researcher"], p)
    import io
    import zipfile

    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "00_제출서류_목록.txt" in names and any("연구계획서" in n for n in names)


def test_revision_drafts_rules(db, users):
    from irb_automation.package import revision as rv
    from irb_automation.workflow import service as wf

    p = db.scalars(select(Protocol).where(Protocol.title.contains("SGLT2"))).one()
    wf.record_submission(db, users["researcher"], p, force=True)
    items = wf.record_revision_request(db, users["researcher"], p,
                                       "1. 동의서에 법정대리인 동의를 추가하십시오.\n2. 자료 보관 기간과 파기 방법을 명시하십시오.")
    drafts = rv.draft_responses(p, items, use_ai=False, db=db, user=users["researcher"])
    assert "consent_form" in drafts[items[0].id]["docs"]
    assert "destruction_method" in drafts[items[1].id]["fields"]
    assert "수정하였습니다" in drafts[items[0].id]["response"]
