from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from irb_automation.engine.facts import ProtocolFacts
from irb_automation.models import Protocol, ProtocolStatus, Role, SubmissionKind
from irb_automation.package.service import plan
from irb_automation.public_irb import checklists, sources
from irb_automation.public_irb.checklist_models import ChecklistDecision, ChecklistDefinition, ChecklistItem
from irb_automation.public_irb.records import RulePack, replay
from irb_automation.public_irb.requirements import compute
from irb_automation.qc.checks import QCDoc
from irb_automation.qc.profile import build_profile
from irb_automation.timeutil import today
from irb_automation.workflow import service as wf
from tests.conftest import csrf, login


def test_public_requirements_follow_profile_and_survive_submission(db, users):
    user = users["researcher"]
    protocol = wf.create_protocol(db, user, {
        "title": "공용 조사 과제", "committee": "public_irb", "access_route": "individual",
        "research_category": "human", "research_method": ["survey"], "consent_routes": ["obtain"],
        "application_route": "new",
        "subject_age_min": 8, "subject_age_max": 14,
    })
    expected = wf.preview(db, protocol)["checklist"]
    by_doc = {item["doc"]: item for item in expected}
    assert by_doc["protocol"]["forms"] == ["권고 제39호"]
    assert by_doc["assent_form"]["forms"] == ["권고 제46호"]
    assert "waiver_request" not in by_doc and "exemption_request" not in by_doc
    assert set(by_doc) <= {item["doc_type"] for item in plan(db, protocol)["items"]}
    profile = build_profile(None, protocol, [QCDoc("a", "계획서.txt", "protocol", "조사")])
    assert profile["committee"] == "public_irb" and profile["method"] == ["survey"]
    sub = wf.record_submission(db, user, protocol, force=True)
    assert sub.snapshot["committee"] == "public_irb" and sub.basis["facts"]["research_method"] == ["survey"]
    assert len(db.scalars(select(RulePack)).all()) == 3
    assert sub.latest_prereview.result["checklist"] == expected
    protocol.research_method = ["records"]
    assert replay(db, sub.basis)["checklist"] == expected
    wf.record_result(db, user, protocol, "expedited_recheck")
    assert protocol.status == ProtocolStatus.SUBMITTED and not sub.closed


def test_parallel_reporting_does_not_replace_primary_submission(db, users):
    user = users["researcher"]
    protocol = Protocol(title="공용 과제", pi_id=user.id, committee="public_irb")
    db.add(protocol)
    db.flush()
    primary = wf.record_submission(db, user, protocol, force=True)
    wf.record_result(db, user, protocol, "approved")
    report = wf.submit_report(db, user, protocol, SubmissionKind.DEVIATION, {"results_summary": "기록"})
    assert protocol.status == ProtocolStatus.APPROVED and protocol.current_submission == primary
    wf.record_result(db, user, protocol, "acknowledged", submission_id=report.id)
    assert report.closed and report.result_entered_by == user.id and protocol.status == ProtocolStatus.APPROVED


def test_public_report_reminder_ends_after_recording_submission(db, users):
    user = users["researcher"]
    protocol = Protocol(title="이해상충 변경", pi_id=user.id, committee="public_irb", status=ProtocolStatus.APPROVED)
    db.add(protocol)
    db.flush()
    event_on = today() - timedelta(days=25)
    wf.track_report_deadline(db, user, protocol, SubmissionKind.COI_UPDATE, event_on)
    assert wf.run_scheduler(db)["report_notices"] == 1
    assert wf.run_scheduler(db)["report_notices"] == 0
    wf.submit_report(db, user, protocol, SubmissionKind.COI_UPDATE, {"event_on": event_on.isoformat()})
    assert "report_notices" not in wf.run_scheduler(db)


def test_reviewer_access_is_scoped(db, users):
    actor, pi, reviewer = users["admin"], users["researcher"], users["researcher2"]
    reviewer.role = Role.REVIEWER
    protocol = Protocol(title="과제", pi_id=pi.id, committee="public_irb")
    db.add(protocol)
    db.flush()
    assert not wf.can_view(db, reviewer, protocol)
    wf.assign_reviewers(db, actor, protocol, [reviewer.id])
    assert wf.can_view(db, reviewer, protocol)
    with pytest.raises(wf.PermissionDenied):
        wf.record_result(db, reviewer, protocol, "approved")
    protocol.co_investigators = [reviewer.id]
    with pytest.raises(wf.PermissionDenied):
        wf.require_reviewer(reviewer, protocol)


def test_checklist_source_verification_and_submitted_edition(db, users):
    admin, researcher, reviewer = users["admin"], users["researcher"], users["reviewer"]
    protocol = Protocol(title="공용 평가", pi_id=researcher.id, committee="public_irb", research_category="stem_cell")
    db.add(protocol)
    db.flush()
    wf.assign_reviewers(db, admin, protocol, [reviewer.id])
    key = sources.SourceKey(series="권고서식", number="10-5", version="2.0")
    source = sources.register_source(db, sources.SourceRegistration(
        key=key, title="테스트 평가표", owner="committee", origin="test", original_filename="checklist.txt",
    ), b"Fixture for source registration")
    with pytest.raises(sources.SourceError):
        sources.activate_source(db, source)
    sources.verify_source(db, source, sources.SourceVerification(verified_by=str(admin.id), manual_note="fixture checked"))
    sources.activate_source(db, source)
    item = ChecklistItem(key="A.1", label="위험 조건이 있는가", favorable="no")
    definition = ChecklistDefinition(source_document_id=source.id, owner="committee", items=[item], reviewed=True)
    form = checklists.register_form(db, admin, definition)
    submission = wf.record_submission(db, researcher, protocol, force=True)
    decision = ChecklistDecision(protocol_id=protocol.id, form_id=form.id, submission_id=submission.id,
                                 item_key=item.key, answer="no", evidence_ref="계획서 3쪽")
    with pytest.raises(wf.PermissionDenied):
        checklists.save_answer(db, researcher, decision)
    source.status = "archived"
    response = checklists.save_answer(db, reviewer, decision)
    assert response.direction == "favorable" and response.decided_by == reviewer.id
    assert protocol.status == ProtocolStatus.SUBMITTED and not submission.closed


@pytest.mark.parametrize(("case", "category", "methods", "routes", "application", "external", "collect",
                          "expected_forms", "expected_docs", "excluded"), [
    (1, "human", ["survey"], ["obtain"], "new", False, True, ["권고 제39호"], {"questionnaire"}, {"waiver_request"}),
    (5, "human", ["survey"], ["waiver_request"], "new", False, True, ["권고 제39호"],
     {"waiver_request", "waiver_self_check"}, {"exemption_request"}),
    (8, "human", ["records"], ["waiver_request"], "new", False, False, ["권고 제43호"],
     {"waiver_request"}, {"questionnaire"}),
    (17, "specimen", ["collect"], ["obtain"], "new", False, True, ["권고 제40호"], {"specimen_consent"}, set()),
    (21, "specimen", ["collect"], ["not_applicable"], "new", True, False, ["권고 제40호"],
     {"mta", "external_irb_approval"}, {"specimen_consent"}),
    (24, "specimen", ["collect"], ["waiver_request"], "new", False, False, ["권고 제40호"],
     {"waiver_request"}, {"exemption_request"}),
    (25, "specimen", ["collect"], ["obtain"], "new", True, True, ["권고 제40호"], {"mta"}, {"specimen_provision"}),
    (26, "specimen", ["collect"], ["obtain"], "new", False, True, ["권고 제40호"], {"specimen_consent"}, {"disposal_transfer"}),
    (29, "stem_cell", ["stem_cell"], ["not_applicable"], "new", True, False, ["권고 제41호"],
     {"external_irb_approval"}, {"consent_form"}),
    (30, "human", ["records"], ["not_applicable"], "exemption", False, False, ["권고 제43호"],
     {"exemption_request", "exemption_self_check"}, {"waiver_request"}),
    (33, "human", ["records"], ["waiver_request"], "exemption", False, False, ["권고 제43호"],
     {"exemption_request", "waiver_request"}, {"questionnaire"}),
])
def test_reference_scenario_requirements(case, category, methods, routes, application, external, collect,
                                         expected_forms, expected_docs, excluded):
    facts = ProtocolFacts(committee="public_irb", research_category=category, research_method=methods,
                          consent_routes=routes, application_route=application, external_transfer=external,
                          existing_data_only=not collect)
    items = {item["doc"]: item for item in compute(facts)}
    assert items["protocol"]["forms"] == expected_forms, case
    assert expected_docs <= items.keys() and excluded.isdisjoint(items), case
    for doc in ("waiver_self_check", "exemption_self_check"):
        if doc in items:
            assert not items[doc]["required"] and not items[doc]["verified"]


def test_public_project_screen_and_historical_requirements(app, db, users):
    researcher = users["researcher"]
    protocol = wf.create_protocol(db, researcher, {
        "title": "공용 조사 과제", "committee": "public_irb", "access_route": "individual",
        "research_category": "human", "research_method": ["survey"], "consent_routes": ["obtain"],
        "application_route": "new",
    })
    db.commit()
    pid = protocol.id
    with TestClient(app) as client:
        login(client, "researcher")
        assert client.get(f"/protocols/{pid}/public-irb").status_code == 200
        assert client.get(f"/protocols/{pid}/online-forms").status_code == 404
        assert "권고 제39호" in client.get(f"/protocols/{pid}/package").text
        assert client.post(f"/protocols/{pid}/public-irb/requirement", follow_redirects=False, data={
            "csrf_token": csrf(client), "doc_type": "pledge", "status": "declared_submitted",
        }).status_code == 303
        assert "제출했다고 표시" in client.get(f"/protocols/{pid}/public-irb").text
        expected = client.get(f"/api/protocols/{pid}/requirements").json()
        assert {x["status"] for x in expected["items"] if x["doc"] == "pledge"} == {"declared_submitted"}
        sub = wf.record_submission(db, researcher, protocol, force=True)
        db.commit()
        past = client.get(f"/api/protocols/{pid}/requirements", params={"submission_id": sub.id})
        assert past.status_code == 200
        assert past.json()["items"] == expected["items"]
        assert client.get(f"/protocols/{pid}/public-irb", params={"submission_id": sub.id}).status_code == 200
        login(client, "researcher2")
        assert client.get(f"/api/protocols/{pid}/requirements").status_code == 404

