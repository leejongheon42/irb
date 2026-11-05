"""위원회 회의, 전자서명, 보완 항목, 변경 대비표, 서류 자동 작성."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from irb_automation import esign
from irb_automation.documents import GENERATABLE, classify_item, generate_and_attach, render
from irb_automation.engine.ruleset import get_ruleset
from irb_automation.models import DecisionType, ProtocolStatus, Recommendation, ReviewType, Role
from irb_automation.workflow import meetings as mt
from irb_automation.workflow import service as wf
from irb_automation.workflow.diff import diff_snapshots

from .conftest import PASSWORD
from .test_workflow import full_docs, get_p

S = ProtocolStatus


def full_board_protocol(db, users):
    p = get_p(db, "SGLT2")
    p.vulnerable_populations = ["children"]
    p.deid_level = "identifiable"
    p.identifiers_collected = ["mrn"]
    p.identifier_justification = "소아 환자 추적을 위해 등록번호가 반드시 필요함을 소명함"
    p.consent_plan = "obtain"
    full_docs(db, p)
    from .test_workflow import add_docs

    add_docs(db, p, "consent_form")
    sub = wf.submit(db, users["researcher"], p)
    assert sub.review_type == ReviewType.FULL
    return p, sub


def test_composition_check_demo_committee(db, users):
    c = mt.composition_check(db)
    assert c["ok"], c["checks"]
    users["reviewer3"].is_external_member = False
    users["reviewer4"].is_external_member = False
    db.flush()
    c = mt.composition_check(db)
    assert not c["ok"] and any(ck["label"] == "기관 비종사 위원" and not ck["ok"] for ck in c["checks"])


def test_meeting_agenda_quorum_vote_and_minutes(db, users):
    coord, chair = users["coordinator"], users["chair"]
    p, sub = full_board_protocol(db, users)
    wf.assign_reviewers(db, coord, p, [users["reviewer1"].id], users["reviewer1"].id)
    wf.submit_review(db, users["reviewer1"], sub.assignments[0], Recommendation.MODIFY, "보호 대책 보완 필요")

    m = mt.create_meeting(db, coord, datetime.now() + timedelta(days=1), "본관 회의실")
    added = mt.build_agenda(db, coord, m)
    assert added["review"] == 1
    item = m.items[0]
    assert item.submission_id == sub.id
    # 다시 불러와도 중복 추가되지 않음
    assert mt.build_agenda(db, coord, m)["review"] == 0

    # 정족수 미충족: 출석 2명(재적 5명)
    mt.set_attendance(db, coord, m, {chair.id, users["reviewer1"].id})
    with pytest.raises(wf.WorkflowError, match="정족수"):
        mt.record_item_decision(db, coord, item, {"modify": 2}, [])

    # 출석 3명이지만 사회적·윤리적 위원 미출석
    mt.set_attendance(db, coord, m, {chair.id, users["reviewer1"].id, users["reviewer2"].id})
    q = mt.quorum_status(db, m, item)
    assert not q["ok"] and any("사회적" in r for r in q["reasons"])

    mt.set_attendance(db, coord, m, {chair.id, users["reviewer1"].id, users["reviewer2"].id, users["reviewer3"].id})
    with pytest.raises(wf.WorkflowError, match="초과"):
        mt.record_item_decision(db, coord, item, {"approve": 5}, [])
    with pytest.raises(wf.WorkflowError, match="과반"):
        mt.record_item_decision(db, coord, item, {"approve": 2, "modify": 2}, [])
    with pytest.raises(wf.WorkflowError, match="의결되지 않은"):
        mt.close_meeting(db, coord, m)
    d = mt.record_item_decision(db, coord, item, {"modify": 3, "approve": 1}, [], conditions="취약군 보호 대책 추가",
                                discussion="소아 자료 최소화 논의")
    assert d.decision == DecisionType.APPROVE_WITH_CONDITIONS
    assert d.review_type == ReviewType.FULL and d.meeting_date == m.scheduled_at.date()
    assert p.status == S.APPROVED
    mt.close_meeting(db, coord, m, "기타 논의 없음")
    assert m.status == "closed"


def test_meeting_recuses_research_team_members(db, users):
    coord = users["coordinator"]
    p, sub = full_board_protocol(db, users)
    # 공동연구자를 위원으로 지정 → 자동 회피
    co = users["researcher2"]
    co.is_committee_member = True
    db.flush()
    m = mt.create_meeting(db, coord, datetime.now(), "")
    mt.build_agenda(db, coord, m)
    assert co.id in m.items[0].recused_ids


def test_report_items_listed_for_expedited_decisions(db, users):
    coord = users["coordinator"]
    p = get_p(db, "SGLT2")
    wf.submit(db, users["researcher"], p)
    wf.decide(db, coord, p, DecisionType.EXEMPT_CONFIRM)
    m = mt.create_meeting(db, coord, datetime.now() + timedelta(days=1), "")
    added = mt.build_agenda(db, coord, m)
    assert added["report"] == 1 and m.items[0].item_type == "report"


def test_esign_hash_detects_change(db, users):
    r = users["researcher"]
    p = get_p(db, "SGLT2")
    sub = wf.submit(db, r, p)
    with pytest.raises(esign.SignatureError):
        esign.sign(db, r, "wrong", "서약", "submission", sub.id, sub.snapshot)
    sig = esign.sign(db, r, PASSWORD, "서약", "submission", sub.id, sub.snapshot)
    assert esign.verify(sig, sub.snapshot)
    assert not esign.verify(sig, {**sub.snapshot, "expected_subjects": 1})


def test_revision_items_block_resubmission_until_answered(db, users):
    r, coord = users["researcher"], users["coordinator"]
    p = get_p(db, "SGLT2")
    wf.submit(db, r, p)
    items = wf.request_revision(db, coord, p, "", items=[("INCLUSION_MISSING", "선정 기준 구체화"), ("", "변수 목록 축소")])
    assert len(items) == 2 and p.status == S.REVISION_REQUESTED
    with pytest.raises(wf.WorkflowError, match="답변"):
        wf.submit(db, r, p)
    wf.respond_revision_items(db, r, p, {items[0].id: "선정기준 4.1절 수정"})
    with pytest.raises(wf.WorkflowError, match="1개 항목"):
        wf.submit(db, r, p)
    wf.respond_revision_items(db, r, p, {items[1].id: "변수 3개 삭제"})
    sub = wf.submit(db, r, p)
    assert sub.kind.value == "resubmission"
    assert all(i.status == "answered" for i in wf.revision_items_for(db, items[0].submission_id))
    with pytest.raises(wf.PermissionDenied):
        wf.respond_revision_items(db, users["reviewer1"], p, {})


def test_diff_snapshots_marks_material_changes(settings):
    rs = get_ruleset()
    old = {"title": "A", "identifiers_collected": [], "expected_subjects": 100, "data_sources": ["emr"]}
    new = {"title": "A", "identifiers_collected": ["mrn"], "expected_subjects": 200, "data_sources": ["emr"]}
    changes = {c["field"]: c for c in diff_snapshots(old, new, rs)}
    assert set(changes) == {"identifiers_collected", "expected_subjects"}
    assert changes["identifiers_collected"]["after"] == "병원등록번호(환자번호)"
    assert changes["identifiers_collected"]["material"]


def test_generated_documents_render_and_attach(db, users):
    p = get_p(db, "SGLT2")
    for dt in GENERATABLE:
        html = render(db, p, dt)
        assert p.title in html and "<html" in html
    deid = render(db, p, "deid_plan")
    assert "연구용 일련번호" in deid or "유지" in deid
    doc = generate_and_attach(db, users["researcher"], p, "waiver_request")
    assert doc.doc_type == "waiver_request" and doc.filename.endswith(".html")
    assert classify_item("생년월일")[1] == "준식별자"
    assert classify_item("환자번호")[1] == "직접 식별자"
    with pytest.raises(wf.PermissionDenied):
        generate_and_attach(db, users["reviewer1"], p, "crf")


def test_team_training_and_multicenter_findings(db, users):
    from irb_automation.engine.prereview import run_prereview

    p = get_p(db, "SGLT2")
    users["researcher2"].training_expires_on = date.today() - timedelta(days=1)
    p.multicenter = True
    p.review_model = "external"
    db.flush()
    result = run_prereview(wf.build_facts(db, p))
    codes = {f["code"] for f in result["findings"]}
    assert {"TEAM_TRAINING_EXPIRED", "SITES_MISSING", "EXTERNAL_IRB_MISSING", "RELIANCE"} <= codes
    assert any(c["doc"] == "external_irb_approval" for c in result["checklist"])
    assert any("타기관 IRB" in r for r in result["classification"]["rationale"])


def test_meeting_pages_over_http(app):
    from fastapi.testclient import TestClient

    from irb_automation.database import SessionLocal

    from .conftest import csrf, login

    with TestClient(app) as c:
        login(c, "coordinator")
        t = csrf(c)
        r = c.post("/meetings", data={"csrf_token": t, "scheduled_at": "2026-12-01T14:00", "location": "회의실"},
                   follow_redirects=True)
        assert "안건을 자동 구성했습니다" in r.text
        with SessionLocal() as db:
            from irb_automation.models import Meeting

            mid = db.scalars(select(Meeting)).one().id
        assert c.get(f"/meetings/{mid}/minutes").status_code == 200
        assert "위원회 구성 요건" in c.get("/meetings/composition").text
        login(c, "researcher")
        assert c.get("/meetings").status_code == 403
        assert db is not None and Role.CHAIR
