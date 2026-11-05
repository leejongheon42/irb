"""반출 심사, DRB 적정성 검토, 계약, 파기, 처리기록 대장, 사전 조회, 지표, 마이그레이션, 메일."""
from __future__ import annotations

import csv
import io
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, select, text

from irb_automation import feasibility
from irb_automation.governance import airlock, records
from irb_automation.metrics import annual_report, kpi
from irb_automation.models import (
    DecisionType,
    DeidJob,
    DestructionRecord,
    FeasibilityQuery,
    ProtocolStatus,
    Recommendation,
    SubmissionKind,
)
from irb_automation.workflow import service as wf

from .test_workflow import full_docs, get_p

S = ProtocolStatus


def approved_protocol(db, users):
    p = get_p(db, "SGLT2")
    wf.submit(db, users["researcher"], p)
    wf.decide(db, users["coordinator"], p, DecisionType.EXEMPT_CONFIRM)
    return p


def to_csv(rows):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode("utf-8")


# ------------------------------------------------------------------ airlock
def test_check_table_small_cells_and_identifiers():
    rows = [["40-49", "M", "12"], ["40-49", "F", "3"], ["50-59", "M", "0"], ["50-59", "F", "25"]]
    c = airlock.check_table(["연령대", "성별", "건수"], rows, {"min_cell": 5, "allow_zero": True,
                                                         "max_rows_aggregate": 1000, "suppression_label": "<5"})
    assert c["verdict"] == "fail" and c["small_cell_count"] == 1 and c["suppressible"]
    out = airlock.suppress(["연령대", "성별", "건수"], rows, c, "<5")
    assert out[1][2] == "<5" and out[0][2] == "12"
    c2 = airlock.check_table(["환자번호", "성명", "HbA1c"], [[str(10000000 + i), "홍길동", "6.1"] for i in range(30)])
    assert c2["verdict"] == "fail" and not c2["suppressible"]
    assert any("식별자" in i for i in c2["issues"])


def test_output_request_flow(db, users):
    p = approved_protocol(db, users)
    raw = to_csv([["연령대", "건수"], ["40-49", "12"], ["50-59", "2"]])
    with pytest.raises(wf.PermissionDenied):
        airlock.submit_output(db, users["reviewer1"], p, "t.csv", raw, "")
    req = airlock.submit_output(db, users["researcher"], p, "table2.csv", raw, "논문 표2")
    assert req.check["verdict"] == "fail" and req.suppressed_path
    with pytest.raises(wf.PermissionDenied):
        airlock.review_output(db, users["researcher"], req, "approve")
    with pytest.raises(wf.WorkflowError, match="억제본"):
        airlock.review_output(db, users["steward"], req, "approve")
    airlock.review_output(db, users["steward"], req, "approve_suppressed", "소규모 셀 억제")
    assert req.status == "approved_suppressed"
    assert airlock.downloadable_path(req) == req.suppressed_path
    content = open(req.suppressed_path, encoding="utf-8-sig").read()
    assert "<5" in content and ",2" not in content


def test_output_requires_active_approval(db, users):
    p = get_p(db, "SGLT2")
    with pytest.raises(wf.WorkflowError, match="유효한 IRB 승인"):
        airlock.submit_output(db, users["researcher"], p, "t.csv", to_csv([["a", "b"], ["x", "10"]]), "")


# ------------------------------------------------------------------ DRB / 계약 / 대장
def make_job(db, users, p, verdict="적정"):
    job = DeidJob(protocol_id=p.id, created_by=users["steward"].id, original_filename="x.csv", input_path="",
                  input_sha256="0" * 64, status="processed",
                  report={"verdict": verdict, "column_actions": [{"column": "환자번호", "action": "pseudonymize_hmac",
                                                                  "description": "가명"}],
                          "output_rows": 10, "processed_at": "2026-11-01T10:00:00"})
    db.add(job)
    db.flush()
    return job


def test_drb_requires_separation_and_reason(db, users):
    p = approved_protocol(db, users)
    job = make_job(db, users, p)
    with pytest.raises(wf.PermissionDenied):
        records.drb_review(db, users["researcher"], job, True)
    users["steward"].role = users["reviewer1"].role  # 처리자가 위원이라도 본인 검토 불가
    with pytest.raises(wf.PermissionDenied, match="처리자 본인"):
        records.drb_review(db, users["steward"], job, True)
    with pytest.raises(wf.WorkflowError, match="사유"):
        records.drb_review(db, users["reviewer1"], job, False, "")
    records.drb_review(db, users["reviewer1"], job, True)
    assert job.drb_status == "approved"
    bad = make_job(db, users, p, "부적정")
    with pytest.raises(wf.WorkflowError):
        records.drb_review(db, users["reviewer1"], bad, True)


def test_transfer_gate_requires_agreement(db, users):
    p = approved_protocol(db, users)
    p.external_transfer = True
    assert records.transfer_gate(db, p)
    with pytest.raises(wf.PermissionDenied):
        records.create_agreement(db, users["researcher"], {"agreement_type": "data_provision", "counterparty": "A"})
    a = records.create_agreement(db, users["steward"], {
        "protocol_id": p.id, "agreement_type": "data_provision", "counterparty": "공동연구기관",
        "start_date": date.today() - timedelta(days=1), "end_date": date.today() + timedelta(days=20)})
    assert records.transfer_gate(db, p) is None
    stats = records.governance_reminders(db)
    assert stats["agreement_notices"] >= 1
    assert records.governance_reminders(db)["agreement_notices"] == 0  # 중복 방지
    records.terminate_agreement(db, users["coordinator"], a)
    assert records.transfer_gate(db, p)


def test_ledger_rows(db, users):
    p = approved_protocol(db, users)
    job = make_job(db, users, p)
    job.purpose = "분석용 제공"
    rows = records.ledger_rows(db)
    assert rows[0]["purpose"] == "분석용 제공" and rows[0]["protocol_no"] == p.protocol_no
    assert "환자번호" in rows[0]["items"] and rows[0]["drb"] == "검토 대기"


# ------------------------------------------------------------------ 파기
def test_closure_creates_destruction_record_and_reminders(db, users):
    r, coord, rev = users["researcher"], users["coordinator"], users["reviewer1"]
    p = get_p(db, "SGLT2")
    p.identifiers_collected = ["mrn"]
    p.identifier_justification = "타 진료과 추적 자료 연계를 위해 등록번호가 반드시 필요함을 소명함"
    full_docs(db, p)
    sub = wf.submit(db, r, p)
    wf.assign_reviewers(db, coord, p, [rev.id], rev.id)
    wf.submit_review(db, rev, sub.assignments[0], Recommendation.APPROVE, "")
    wf.decide(db, coord, p, DecisionType.APPROVE)
    wf.submit_report(db, r, p, SubmissionKind.FINAL_REPORT, {"results_summary": "결과 요약을 기재함", "data_destroyed": False})
    wf.decide(db, coord, p, DecisionType.ACKNOWLEDGE)
    rec = db.scalars(select(DestructionRecord).where(DestructionRecord.protocol_id == p.id)).one()
    assert rec.destroyed_at is None and rec.due_date >= date.today() + timedelta(days=365 * 3 - 1)
    rec.due_date = date.today() + timedelta(days=5)
    assert records.governance_reminders(db)["destruction_notices"] >= 1
    with pytest.raises(wf.WorkflowError):
        records.certify_destruction(db, r, rec, date.today(), "")
    with pytest.raises(wf.WorkflowError, match="허용되지 않는"):
        records.certify_destruction(db, r, rec, date.today(), "삭제", ("cert.html", b"<script>"))
    records.certify_destruction(db, r, rec, date.today(), "분석서버 영구 삭제", ("cert.pdf", b"%PDF"))
    assert rec.destroyed_at == date.today() and rec.certificate_path


# ------------------------------------------------------------------ feasibility
def test_feasibility_demo_connector_suppression(db, users, tmp_path):
    path = tmp_path / "cdw.csv"
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["환자번호", "생년월일", "성별", "입원일자", "진단코드"])
        for i in range(57):
            w.writerow([str(i), "1960-01-01", "M" if i % 2 else "F", "2023-03-01", "E11.9"])
        for i in range(3):
            w.writerow([f"r{i}", "2010-01-01", "F", "2023-03-01", "Q90"])
    conn = feasibility.DemoCsvConnector(path)
    r = feasibility.run_query(db, users["researcher"], feasibility.Criteria(dx_codes=["E11"]), conn)
    assert r["display"] == "약 60"
    r = feasibility.run_query(db, users["researcher"], feasibility.Criteria(dx_codes=["Q90"]), conn)
    assert r["display"] == "<10"
    assert conn.count(feasibility.Criteria(dx_codes=["E11"], sex="M", age_min=60, age_max=70)) == 28
    assert db.scalar(select(FeasibilityQuery.id).where(FeasibilityQuery.user_id == users["researcher"].id))
    with pytest.raises(feasibility.FeasibilityError):
        feasibility.Criteria(dx_codes=["E11; DROP"]).validate()


def test_feasibility_daily_limit(db, users, tmp_path, monkeypatch):
    path = tmp_path / "cdw.csv"
    path.write_text("환자번호,생년월일,성별,입원일자,진단코드\n1,1960-01-01,M,2023-01-01,E11\n", encoding="utf-8")
    conn = feasibility.DemoCsvConnector(path)
    from irb_automation.engine import ruleset as rsmod

    rs = rsmod.get_ruleset()
    monkeypatch.setitem(rs.data, "feasibility_policy", {"min_cell": 10, "rounding": 10, "daily_limit": 2})
    for _ in range(2):
        feasibility.run_query(db, users["researcher"], feasibility.Criteria(dx_codes=["E11"]), conn)
    with pytest.raises(feasibility.FeasibilityError, match="한도"):
        feasibility.run_query(db, users["researcher"], feasibility.Criteria(dx_codes=["E11"]), conn)


def test_omop_connector_against_sqlite(tmp_path):
    url = f"sqlite:///{tmp_path / 'omop.db'}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE person (person_id INTEGER, gender_concept_id INTEGER, year_of_birth INTEGER)"))
        c.execute(text("CREATE TABLE condition_occurrence (person_id INTEGER, condition_source_value TEXT, condition_start_date DATE)"))
        for i in range(30):
            c.execute(text("INSERT INTO person VALUES (:i, :g, :y)"), {"i": i, "g": 8507 if i < 20 else 8532, "y": 1960 if i < 25 else 2015})
            c.execute(text("INSERT INTO condition_occurrence VALUES (:i, :c, :d)"),
                      {"i": i, "c": "E11.9" if i < 28 else "I10", "d": "2023-05-01"})
            c.execute(text("INSERT INTO condition_occurrence VALUES (:i, 'E11.9', '2024-01-01')"), {"i": i}) if i < 5 else None
    conn = feasibility.OmopConnector(url)
    assert conn.count(feasibility.Criteria(dx_codes=["E11"])) == 28
    assert conn.count(feasibility.Criteria(dx_codes=["E11"], sex="M")) == 20
    assert conn.count(feasibility.Criteria(dx_codes=["E11"], age_min=19)) == 25
    assert conn.count(feasibility.Criteria(dx_codes=["E11", "I10"], date_from=date(2023, 1, 1), date_to=date(2023, 12, 31))) == 30


# ------------------------------------------------------------------ metrics
def test_kpi_splits_irb_and_researcher_time(db, users):
    r, coord = users["researcher"], users["coordinator"]
    p = get_p(db, "SGLT2")
    s1 = wf.submit(db, r, p)
    wf.request_revision(db, coord, p, "보완 필요")
    s1.submitted_at = s1.submitted_at - timedelta(days=10)
    s1.closed_at = s1.submitted_at + timedelta(days=4)  # IRB 4일
    s2 = wf.submit(db, r, p)
    s2.submitted_at = s1.closed_at + timedelta(days=3)  # 연구자 3일
    d = wf.decide(db, coord, p, DecisionType.EXEMPT_CONFIRM)
    d.decided_at = s2.submitted_at + timedelta(days=2)  # IRB 2일
    s2.closed_at = d.decided_at
    db.flush()
    k = kpi(db, date.today() - timedelta(days=30), date.today())
    cyc = k["cycles"][0]
    assert cyc["irb"] == 6.0 and cyc["researcher"] == 3.0 and cyc["total"] == 9.0 and cyc["rounds"] == 2
    assert k["turnaround"]["exempt"]["first_round_rate"] == 0.0
    assert k["classification"]["agreement_rate"] == 100.0
    rep = annual_report(db, date.today().year)
    assert rep["decisions_total"] == 1 and rep["composition"]["ok"]


# ------------------------------------------------------------------ 마이그레이션 / 메일
def test_migrate_columns_adds_missing(settings, tmp_path):
    from irb_automation.database import Base, migrate_columns

    url = f"sqlite:///{tmp_path / 'old.db'}"
    eng = create_engine(url)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64), password_hash VARCHAR(256), "
                       "full_name VARCHAR(100), department VARCHAR(100), email VARCHAR(200), role VARCHAR(32), "
                       "training_expires_on DATE, is_active BOOLEAN, created_at DATETIME)"))
        c.execute(text("INSERT INTO users (id, username, role) VALUES (1, 'old', 'researcher')"))
    Base.metadata.create_all(eng)
    added = migrate_columns(eng)
    assert "users.is_committee_member" in added
    with eng.connect() as c:
        assert c.execute(text("SELECT is_committee_member, gender FROM users")).one() == (0, "")
    assert migrate_columns(eng) == []


def test_email_sent_when_smtp_configured(db, users, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            pass

        def login(self, u, p):
            pass

        def send_message(self, msg):
            sent.append(("send", msg["To"], msg["Subject"]))

    import irb_automation.notifications as n

    monkeypatch.setenv("IRB_SMTP_HOST", "smtp.local")
    monkeypatch.setattr(n.smtplib, "SMTP", FakeSMTP)
    users["researcher"].email = "pi@example.org"
    threads = []
    orig = n.send_email_async
    monkeypatch.setattr(n, "send_email_async", lambda *a, **k: threads.append(orig(*a, **k)))
    n.notify(db, [users["researcher"]], "테스트 알림", "/")
    for t in threads:
        t.join(5)
    assert ("send", "pi@example.org", "[IRB] 테스트 알림") in sent
