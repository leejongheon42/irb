from __future__ import annotations

import pytest

from irb_automation.qc.checks import QCDoc, run_rules


@pytest.mark.parametrize("corrected", [False, True])
def test_blood_collection_limits_are_compared_by_per_draw_and_participant_total(corrected: bool) -> None:
    per_draw, total = ("15", "45") if corrected else ("20", "60")
    docs = [
        QCDoc("p", "계획서.pdf", "protocol", "V1, V2, V5에서 정맥혈을 회당 최대 15 mL, 총 최대 45 mL 채취한다."),
        QCDoc("c", "설명문.pdf", "consent_form",
              f"혈액은 1일, 4주, 48주에 회당 최대 {per_draw} mL, 총 최대 {total} mL 채취합니다."),
        QCDoc("r", "기록서.pdf", "crf", "V1 방문 기록\n채혈량\n12 mL\n필요 시 추가 채혈은 회당 최대 5 mL로 제한한다."),
    ]

    findings, _ = run_rules(docs, None)

    collection = [issue for issue in findings if issue["code"] == "CROSS_SAMPLE_VOLUME"]
    assert all(issue["result"] == "mismatch" for issue in collection)
    observed = {issue["scope"]: {loc["doc"]: loc["norm"] for loc in issue["locations"]}
                for issue in collection}
    assert observed == ({} if corrected else {
        "회당 최대 채혈": {"p": "15mL", "c": "20mL"},
        "참여자별 총 채혈": {"p": "45mL", "c": "60mL"},
    })


@pytest.mark.parametrize("corrected", [False, True])
def test_visit_payment_terms_follow_their_scope_across_pdf_lines(corrected: bool) -> None:
    per_visit, total = ("30,000", "150,000") if corrected else ("50,000", "250,000")
    docs = [
        QCDoc("p", "계획서.pdf", "protocol", "방문당 30,000원, 계획된 5회 방문 최대 150,000원."),
        QCDoc("c", "설명문.pdf", "consent_form", "방문당 30,000원,\n총 5회 방문 최대 150,000원을 지급합니다."),
        QCDoc("n", "공고문.pdf", "recruitment", f"참여 비용: 검사 무료. 방문당 {per_visit}원, 최대 {total}원."),
        QCDoc("b", "예산.pdf", "budget", "손상 보상은 방문당 10,000원, 최대 20,000원. "
              "선별탈락 시 방문당 8,000원. 정규 방문 사례비: 방문당 30,000원, 정규 방문 최대 150,000원.\n"
              "외래 접수 시 방문당 12,000원을 청구합니다.\n예산 총액 33,600,000원"),
    ]

    findings, _ = run_rules(docs, None)

    payments = [issue for issue in findings if issue["code"] == "CROSS_EXPENSE"]
    assert all(issue["result"] == "mismatch" for issue in payments)
    observed = {issue["scope"]: {loc["doc"]: loc["norm"] for loc in issue["locations"]}
                for issue in payments}
    assert observed == ({} if corrected else {
        "방문당 참여 지급액": {"p": "30000", "c": "30000", "n": "50000", "b": "30000"},
        "참여 지급액 상한": {"p": "150000", "c": "150000", "n": "250000", "b": "150000"},
    })
