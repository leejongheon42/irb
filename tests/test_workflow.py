"""연구자 과제 진행: 작성 → e-IRB 제출 기록 → 보완 요청 입력·답변 → 결과 입력 → 변경·지속·종료, 만료 알림."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from irb_automation.models import Notification, Protocol, ProtocolStatus, SubmissionKind
from irb_automation.workflow import service as wf

S = ProtocolStatus


def _protocol(db, users, **kw) -> Protocol:
    base = dict(
        title="후향적 당뇨 코호트 연구", pi_id=users["researcher"].id, department="내분비내과",
        study_type="cdw_analysis", objectives="SGLT2 억제제와 급성 신손상 연관성 평가", methods="코호트 분석" * 5,
        target_population="성인 제2형 당뇨병", inclusion_criteria="만 19세 이상 제2형 당뇨병 환자",
        expected_subjects=500, sample_size_justification="연구 기간 내 적격 환자 전수",
        study_start=date.today() + timedelta(days=10), study_end=date.today() + timedelta(days=400),
        data_sources=["cdw"], data_period_start=date(2020, 1, 1), data_period_end=date(2024, 12, 31),
        data_items="연령\n성별\nHbA1c", deid_level="pseudonymized", deid_performed_by="data_steward",
        access_controls="원내 분석서버 전용, 접근 로그 보관, 반출 금지", retention_years=3,
        destruction_method="연구 종료 3년 후 영구 삭제", consent_plan="waiver",
        waiver_impracticable_reasons=["large_population"], waiver_impracticable_detail="대규모 과거 자료로 동의 획득 불가",
        waiver_no_presumed_refusal=True, waiver_minimal_risk_detail="가명정보만 이용하여 위험이 극히 낮음",
    )
    base.update(kw)
    p = Protocol(**base)
    db.add(p)
    db.flush()
    return p


def test_record_submission_requires_member_and_force_for_criticals(db, users):
    p = _protocol(db, users, objectives="")
    with pytest.raises(wf.PermissionDenied):
        wf.record_submission(db, users["researcher2"], p)
    result = wf.preview(db, p)
    if not result["ready_for_review"]:
        with pytest.raises(wf.WorkflowError, match="필수 보완"):
            wf.record_submission(db, users["researcher"], p)
    sub = wf.record_submission(db, users["researcher"], p, external_ref="E-1", force=True)
    assert p.status == S.SUBMITTED and sub.external_ref == "E-1" and sub.latest_prereview is not None
    assert not p.is_editable


def test_revision_request_split_and_resubmit(db, users):
    r = users["researcher"]
    p = _protocol(db, users)
    wf.record_submission(db, r, p, force=True)
    items = wf.record_revision_request(db, r, p, "1. 동의 면제 사유를 구체화하십시오.\n2. 보관 기간을 명시하십시오.\n   (3년 이상)")
    assert [i.request_text for i in items] == ["동의 면제 사유를 구체화하십시오.", "보관 기간을 명시하십시오. (3년 이상)"]
    assert p.status == S.REVISION_REQUESTED and p.is_editable
    assert p.current_submission.result == "revision"
    with pytest.raises(wf.WorkflowError, match="답변"):
        wf.record_submission(db, r, p)
    wf.respond_revision_items(db, r, p, {i.id: "반영함" for i in items})
    sub = wf.record_submission(db, r, p, force=True)
    assert sub.kind == SubmissionKind.RESUBMISSION and p.status == S.SUBMITTED


def test_results_approve_amend_continue_close(db, users):
    r = users["researcher"]
    p = _protocol(db, users)
    wf.record_submission(db, r, p, force=True)
    with pytest.raises(wf.WorkflowError):
        wf.record_result(db, r, p, "acknowledged")
    sub = wf.record_result(db, r, p, "approved", irb_no="IRB-2026-0099", note="승인")
    assert p.status == S.APPROVED and p.protocol_no == "IRB-2026-0099" and p.expires_at
    assert sub.result_label == "승인" and sub.closed
    first_end = p.expires_at
    # 변경
    wf.start_amendment(db, r, p)
    assert p.status == S.AMENDMENT
    wf.update_protocol(db, r, p, {"expected_subjects": 700})
    sub = wf.record_submission(db, r, p, force=True)
    assert sub.kind == SubmissionKind.AMENDMENT and p.version == 2
    wf.record_result(db, r, p, "disapproved")
    assert p.status == S.APPROVED and p.expected_subjects == 500 and p.version == 1  # 부결 시 승인본 복원
    # 지속심의
    sub = wf.submit_report(db, r, p, SubmissionKind.CONTINUING, {"progress_summary": "진행 중"})
    wf.record_result(db, r, p, "approved")
    assert p.status == S.APPROVED and p.expires_at >= first_end
    # 종료
    wf.submit_report(db, r, p, SubmissionKind.FINAL_REPORT, {"results_summary": "완료"})
    wf.record_result(db, r, p, "acknowledged")
    assert p.status == S.CLOSED


def test_exempt_and_cancel_amendment(db, users):
    r = users["researcher"]
    p = _protocol(db, users)
    wf.record_submission(db, r, p, force=True)
    wf.record_result(db, r, p, "exempt")
    assert p.status == S.EXEMPT and p.is_active_approval
    wf.start_amendment(db, r, p)
    wf.update_protocol(db, r, p, {"title": "바뀐 제목"})
    wf.cancel_amendment(db, r, p)
    assert p.status == S.EXEMPT and p.title == "후향적 당뇨 코호트 연구"


def test_scheduler_expiry_notices(db, users):
    r = users["researcher"]
    p = _protocol(db, users)
    wf.record_submission(db, r, p, force=True)
    wf.record_result(db, r, p, "approved", approval_end=date.today() + timedelta(days=20))
    stats = wf.run_scheduler(db)
    assert stats["expiry_notices"] == 1
    assert wf.run_scheduler(db)["expiry_notices"] == 0  # 중복 알림 없음
    p.expires_at = date.today() - timedelta(days=1)
    assert wf.run_scheduler(db)["expired"] == 1 and p.status == S.EXPIRED
    assert db.query(Notification).filter(Notification.user_id == r.id).count() >= 2


def test_can_view(db, users):
    p = _protocol(db, users)
    assert wf.can_view(db, users["researcher"], p)
    assert wf.can_view(db, users["admin"], p)
    assert not wf.can_view(db, users["researcher2"], p)
