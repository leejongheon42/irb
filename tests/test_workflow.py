from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from irb_automation import audit
from irb_automation.models import (
    AuditLog,
    DecisionType,
    Document,
    Notification,
    Protocol,
    ProtocolStatus,
    Recommendation,
    ReviewType,
    SubmissionKind,
)
from irb_automation.workflow import service as wf

S = ProtocolStatus


def add_docs(db, p, *types):
    for t in types:
        db.add(Document(protocol_id=p.id, doc_type=t, filename=f"{t}.pdf", stored_path="", sha256="0" * 64,
                        size=0, uploaded_by=p.pi_id))
    db.flush()
    db.refresh(p)


def full_docs(db, p):
    add_docs(db, p, "application", "exemption_request", "protocol", "cv", "training_cert", "coi", "crf",
             "deid_plan", "security_pledge", "waiver_request")


def get_p(db, title_part):
    return db.scalars(select(Protocol).where(Protocol.title.contains(title_part))).one()


def test_submit_blocked_until_critical_resolved(db, users):
    p = get_p(db, "SGLT2")
    p.documents.clear()
    db.flush()
    with pytest.raises(wf.WorkflowError, match="필수 보완"):
        wf.submit(db, users["researcher"], p)
    assert p.status == S.DRAFT and p.protocol_no is None


def test_exempt_path(db, users):
    p = get_p(db, "SGLT2")
    sub = wf.submit(db, users["researcher"], p)
    assert sub.kind == SubmissionKind.INITIAL
    assert sub.review_type == ReviewType.EXEMPT
    assert p.protocol_no.startswith(f"IRB-{date.today().year}-")
    assert p.status == S.SUBMITTED
    # 사무국에 알림
    assert db.scalars(select(Notification).where(Notification.user_id == users["coordinator"].id)).first()

    d = wf.decide(db, users["coordinator"], p, DecisionType.EXEMPT_CONFIRM)
    assert p.status == S.EXEMPT and p.is_active_approval
    assert d.letter_no == f"{p.protocol_no}-01"
    assert p.expires_at == p.study_end


def test_full_lifecycle_with_amendment_continuing_and_closure(db, users):
    r, coord, chair, rev1, rev2 = (users[k] for k in ("researcher", "coordinator", "chair", "reviewer1", "reviewer2"))
    p = get_p(db, "SGLT2")
    p.identifiers_collected = ["mrn"]
    p.identifier_justification = "타 진료과 추적 자료 연계를 위해 등록번호가 반드시 필요함을 소명함"
    full_docs(db, p)
    sub = wf.submit(db, r, p)
    assert sub.review_type == ReviewType.EXPEDITED

    # 연구팀은 심의위원이 될 수 없음
    with pytest.raises(wf.WorkflowError, match="이해상충"):
        wf.assign_reviewers(db, coord, p, [users["researcher2"].id], None)
    wf.assign_reviewers(db, coord, p, [rev1.id, rev2.id], rev1.id)
    assert p.status == S.UNDER_REVIEW

    # 의견 없이 승인 불가
    with pytest.raises(wf.WorkflowError, match="의견"):
        wf.decide(db, coord, p, DecisionType.APPROVE)
    a1 = next(a for a in sub.assignments if a.reviewer_id == rev1.id)
    with pytest.raises(wf.PermissionDenied):
        wf.submit_review(db, rev2, a1, Recommendation.APPROVE, "")
    wf.submit_review(db, rev1, a1, Recommendation.APPROVE, "")
    d = wf.decide(db, coord, p, DecisionType.APPROVE)
    assert p.status == S.APPROVED
    first_expiry = p.expires_at
    assert first_expiry == min(date.today() + timedelta(days=365), p.study_end)
    assert d.approval_end == first_expiry

    # 변경심의 - 기존 유효기간 유지, 버전 증가
    wf.start_amendment(db, r, p)
    assert p.status == S.AMENDMENT and p.is_active_approval
    wf.update_protocol(db, r, p, {"expected_subjects": 9000})
    amend = wf.submit(db, r, p)
    assert amend.kind == SubmissionKind.AMENDMENT and p.version == 2
    wf.assign_reviewers(db, coord, p, [rev1.id], rev1.id)
    wf.submit_review(db, rev1, amend.assignments[0], Recommendation.APPROVE, "")
    wf.decide(db, coord, p, DecisionType.APPROVE)
    assert p.status == S.APPROVED and p.expires_at == first_expiry and p.expected_subjects == 9000

    # 지속심의 - 유효기간 연장
    cont = wf.submit_report(db, r, p, SubmissionKind.CONTINUING,
                            {"progress_summary": "자료 추출 완료, 1차 분석을 진행 중이며 계획 위반 사항 없음", "subjects_analyzed": "8000"})
    assert cont.review_type == ReviewType.EXPEDITED
    wf.assign_reviewers(db, coord, p, [rev2.id], rev2.id)
    wf.submit_review(db, rev2, cont.assignments[0], Recommendation.APPROVE, "")
    wf.decide(db, coord, p, DecisionType.APPROVE)
    # 연장되지만 연구 종료 예정일을 넘지 않는다
    assert p.expires_at == min(first_expiry + timedelta(days=365), p.study_end) and p.expires_at > first_expiry

    # 종료보고
    wf.submit_report(db, r, p, SubmissionKind.FINAL_REPORT, {"results_summary": "연구 결과 요약", "data_destroyed": True})
    with pytest.raises(wf.WorkflowError):
        wf.decide(db, coord, p, DecisionType.APPROVE)
    wf.decide(db, chair, p, DecisionType.ACKNOWLEDGE)
    assert p.status == S.CLOSED and not p.is_active_approval
    assert len(p.submissions) == 4


def test_revision_and_resubmission(db, users):
    r, coord = users["researcher"], users["coordinator"]
    p = get_p(db, "SGLT2")
    wf.submit(db, r, p)
    with pytest.raises(wf.PermissionDenied):
        wf.request_revision(db, r, p, "x")
    wf.request_revision(db, coord, p, "선정기준의 진단코드를 명확히 하십시오.")
    assert p.status == S.REVISION_REQUESTED and p.is_editable
    wf.update_protocol(db, r, p, {"inclusion_criteria": "만 19세 이상, KCD E11.x 진단, 연구 약물 신규 처방자로 한정함"})
    sub = wf.submit(db, r, p)
    assert sub.kind == SubmissionKind.RESUBMISSION


def test_full_board_requires_meeting_date(db, users):
    r, coord, chair, rev1 = (users[k] for k in ("researcher", "coordinator", "chair", "reviewer1"))
    p = get_p(db, "SGLT2")
    p.vulnerable_populations = ["children"]
    p.deid_level = "identifiable"
    p.identifiers_collected = ["mrn"]
    p.identifier_justification = "소아 환자 추적을 위해 등록번호가 반드시 필요함을 소명함"
    p.consent_plan = "obtain"
    full_docs(db, p)
    add_docs(db, p, "consent_form")
    sub = wf.submit(db, r, p)
    assert sub.review_type == ReviewType.FULL
    wf.assign_reviewers(db, coord, p, [rev1.id], rev1.id)
    wf.submit_review(db, rev1, sub.assignments[0], Recommendation.MODIFY, "취약군 보호 대책 보완")
    with pytest.raises(wf.WorkflowError, match="회의일자"):
        wf.decide(db, chair, p, DecisionType.APPROVE_WITH_CONDITIONS, conditions="보호대책 추가")
    d = wf.decide(db, chair, p, DecisionType.APPROVE_WITH_CONDITIONS, conditions="보호대책 추가", meeting_date=date.today())
    assert d.review_type == ReviewType.FULL and p.status == S.APPROVED


def test_disapproved_amendment_restores_approved_content(db, users):
    r, coord, rev1 = users["researcher"], users["coordinator"], users["reviewer1"]
    p = get_p(db, "SGLT2")
    wf.submit(db, r, p)
    wf.decide(db, coord, p, DecisionType.EXEMPT_CONFIRM)
    wf.start_amendment(db, r, p)
    wf.update_protocol(db, r, p, {"title": "변경된 제목으로 연구 범위 확대", "expected_subjects": 50000})
    wf.submit(db, r, p)
    wf.assign_reviewers(db, coord, p, [rev1.id], rev1.id)
    wf.decide(db, coord, p, DecisionType.DISAPPROVE, conditions="범위 확대 근거 부족")
    assert p.status == S.EXEMPT
    assert "SGLT2" in p.title and p.expected_subjects == 8500 and p.version == 1


def test_cancel_amendment(db, users):
    r, coord = users["researcher"], users["coordinator"]
    p = get_p(db, "SGLT2")
    wf.submit(db, r, p)
    wf.decide(db, coord, p, DecisionType.EXEMPT_CONFIRM)
    wf.start_amendment(db, r, p)
    wf.update_protocol(db, r, p, {"expected_subjects": 1})
    wf.cancel_amendment(db, r, p)
    assert p.status == S.EXEMPT and p.expected_subjects == 8500


def test_scheduler_expires_and_notifies_once(db, users):
    r, coord, rev1 = users["researcher"], users["coordinator"], users["reviewer1"]
    p = get_p(db, "SGLT2")
    p.identifiers_collected = ["mrn"]
    p.identifier_justification = "타 진료과 추적 자료 연계를 위해 등록번호가 반드시 필요함을 소명함"
    full_docs(db, p)
    sub = wf.submit(db, r, p)
    wf.assign_reviewers(db, coord, p, [rev1.id], rev1.id)
    wf.submit_review(db, rev1, sub.assignments[0], Recommendation.APPROVE, "")
    wf.decide(db, coord, p, DecisionType.APPROVE)

    p.expires_at = date.today() + timedelta(days=5)
    s1 = wf.run_scheduler(db)
    s2 = wf.run_scheduler(db)
    assert s1["expiry_notices"] >= 1 and s2["expiry_notices"] == 0  # 중복 알림 방지

    p.expires_at = date.today() - timedelta(days=1)
    stats = wf.run_scheduler(db)
    assert stats["expired"] == 1 and p.status == S.EXPIRED and not p.is_active_approval
    # 만료 후 지속심의 신청 가능
    cont = wf.submit_report(db, r, p, SubmissionKind.CONTINUING, {"progress_summary": "만료 기간 중 연구 활동 없음을 보고합니다. 분석 재개 예정"})
    assert any(f["code"] == "APPROVAL_LAPSED" for f in cont.latest_prereview.result["findings"])


def test_overdue_reviewer_notification(db, users):
    r, coord, rev1 = users["researcher"], users["coordinator"], users["reviewer1"]
    p = get_p(db, "SGLT2")
    wf.submit(db, r, p)
    wf.assign_reviewers(db, coord, p, [rev1.id], rev1.id, due_date=date.today() - timedelta(days=1))
    assert wf.run_scheduler(db)["overdue_notices"] == 1


def test_audit_chain_detects_tampering(db, users):
    p = get_p(db, "SGLT2")
    wf.submit(db, users["researcher"], p)
    db.flush()
    assert audit.verify_chain(db) == (True, None)
    entry = db.scalars(select(AuditLog).where(AuditLog.action == "submit")).first()
    entry.details = {"kind": "tampered"}
    db.flush()
    ok, broken = audit.verify_chain(db)
    assert not ok and broken == entry.id


def test_reviewer_cannot_see_unassigned_protocol(db, users):
    p = get_p(db, "SGLT2")
    assert not wf.can_view(db, users["reviewer1"], p)
    assert wf.can_view(db, users["researcher2"], p)  # 공동연구자
    assert not wf.can_view(db, users["steward"], p)  # 승인 전
