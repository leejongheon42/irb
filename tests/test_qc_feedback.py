from fastapi.testclient import TestClient

from irb_automation.database import SessionLocal
from irb_automation.models import Protocol
from irb_automation.qc import checks, service
from irb_automation.qc.eval import evaluate_decisions
from irb_automation.qc.grouping import merge_ai_evidence
from irb_automation.qc.models import QCRun
from irb_automation.qc.profile import build_profile
from irb_automation.qc.review import refresh, transition

from .conftest import csrf, login


def test_review_feedback_decision_regression():
    results = evaluate_decisions()
    assert results["failed"] == []
    assert results["false_positives"] == 0


def test_ai_duplicate_quotes_are_added_without_duplicate_candidate(monkeypatch):
    from irb_automation.ai import client
    from irb_automation.ai.privacy import mask_text
    from irb_automation.qc.ai_phase import run_ai
    from irb_automation.qc.units import annotate

    docs = [checks.QCDoc("p", "계획서.txt", "protocol", "연구대상자 수: 총 120명 (문의 010-1234-5678)"),
            checks.QCDoc("c", "동의서.txt", "consent_form", "연구대상자 수: 총 80명")]
    quote = mask_text(docs[0].text).text
    assert quote != docs[0].text
    monkeypatch.setattr(client, "ai_enabled", lambda: True)
    monkeypatch.setattr(client, "structured_call", lambda **kwargs: {
        "summary": "", "issues": [{"doc_index": 0, "category": "cross", "severity": "major",
                                   "rule_id": "E02", "flag": "대상자 수", "title": "대상자 수 확인",
                                   "locations": [{"doc_index": 0, "quote": "연구대상자 수: 총 120명"},
                                                 {"doc_index": 1, "quote": docs[1].text}]},
                                  {"doc_index": 0, "category": "context", "severity": "major",
                                   "rule_id": "M10", "flag": "서술 모순", "title": "인용 확인",
                                   "locations": [{"doc_index": 0, "quote": quote}]}],
    })
    found, _ = checks.run_rules(docs, None)
    issues, _ = run_ai(docs, found)
    assert all(i["confidence"] == "ai_verified" for i in issues)
    keep = merge_ai_evidence(found, issues)
    assert len(keep) == 1 and keep[0]["locations"][0]["quote"] == quote
    cross = next(annotate(i) for i in found if i["code"] == "CROSS_SUBJECTS")
    assert len(cross["ai_evidence"]) == 2 and len(cross["locations"]) == 2


def test_record_value_does_not_charge_three_times(db, users):
    user = users["researcher"]
    protocol = Protocol(title="Test", pi_id=user.id, expected_subjects=120)
    db.add(protocol)
    db.flush()
    docs = [checks.QCDoc("a", "계획서_v1.txt", "protocol", "버전: v1.0\n연구대상자 수: 총 100명"),
            checks.QCDoc("b", "동의서_v1.txt", "consent_form", "버전: v1.0\n연구대상자 수: 총 80명")]
    qr = service.run(db, user, docs, protocol=protocol, use_ai=False, profile={"compare_only": True})
    issue, = [i for i in qr.issues if i["code"] in ("CROSS_SUBJECTS", "RECORD_SUBJECTS")]
    assert sorted(loc["norm"] for loc in issue["locations"]) == ["100", "120", "80"]
    assert not any(i.get("rule_id") == "E10" for i in qr.issues)
    qr.issues = [transition(i, {"action": "confirmed", "severity": "", "note": ""}) if i is issue else i
                 for i in qr.issues]
    refresh(qr)
    assert qr.score == 93 and qr.counts["major"] == 1


def test_provisional_profile_does_not_infer_excluded_groups():
    doc = checks.QCDoc("a", "계획서.txt", "protocol",
                       "시험약 투여 용량은 표 3 참조. 의무기록을 검토한다.\n제외 기준: 만 19세 미만 아동은 제외한다.\n혈액 검체를 채취한다.")
    result = build_profile(None, None, [doc])
    assert "drug" not in result and "vulnerable" not in result
    assert result["study_type"] == "unknown"
    consent = checks.QCDoc("b", "동의서.txt", "consent_form", "1회 100mg 투여")
    result = build_profile(None, None, [doc, consent])
    assert result["drug"] is True and "100mg" in result["evidence"]["drug"]


def test_confirmation_requires_resolving_alias_and_saves_history(app):
    with TestClient(app) as c:
        login(c, "researcher")
        token = csrf(c)
        files = [("files", ("예산서_A_v1.0.txt", "버전: v1.0\n시험약: ABC정".encode(), "text/plain")),
                 ("files", ("예산서_B_v1.0.txt", "버전: v1.0\n시험약: XYZ정".encode(), "text/plain"))]
        upload = c.post("/qc", files=files, data={"csrf_token": token, "compare_only": "yes"}, follow_redirects=False)
        loc = upload.headers["location"]
        rid = int(loc.rsplit("/", 1)[-1])
        assert "확인 결과 저장" in c.get(loc).text
        with SessionLocal() as db:
            run = db.get(QCRun, rid)
            issue, = [i for i in run.issues if i["code"] == "CROSS_DRUG"]
            assert run.verdict == "HOLD" and run.score == 100
            initial_coverage = run.counts["coverage"]
            assert all(unit["rule"] != "E10" for unit in run.checks)
        path = f"{loc}/issues/{issue['id']}"
        form = {"csrf_token": token, "action": "confirmed"}
        assert c.post(f"{path}/review", data=form, follow_redirects=False).status_code == 400
        answer = {"csrf_token": token, "answer": "different_entity", "note": "시험약 성분이 서로 다릅니다."}
        assert c.post(f"{path}/resolve-check", data=answer, follow_redirects=False).status_code == 303
        assert c.post(f"{path}/review", data=form, follow_redirects=False).status_code == 303
        with SessionLocal() as db:
            run = db.get(QCRun, rid)
            assert run.verdict == "FAIL" and run.score == 85
            assert run.issue(issue["id"])["check_answer"]["by"] == run.created_by
            assert run.counts["coverage"] > initial_coverage
        rerun = c.post(f"{loc}/profile", data={"csrf_token": token, "drug": "no", "compare_only": "yes",
                                               "committee": "public_irb"}, follow_redirects=False)
        assert rerun.status_code == 303
        with SessionLocal() as db:
            new = db.get(QCRun, int(rerun.headers["location"].rsplit("/", 1)[-1]))
            assert new.verdict == "PASS" and new.counts["coverage"] == 1
            assert [d["path"] for d in new.docs] == [d["path"] for d in run.docs]
