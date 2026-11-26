from __future__ import annotations

import pytest

from irb_automation.qc.checks import QCDoc, run_rules


@pytest.mark.parametrize(("outcome", "dependent", "expected"), [
    ("LVEF", "EROA", True),
    ("EROA", "EROA", False),
    ("LVEF", "LVEF", False),
])
def test_primary_endpoint_is_checked_against_the_objective_and_statistical_outcome(
    outcome: str, dependent: str, expected: bool,
) -> None:
    plan = QCDoc("p", "연구계획서.pdf", "protocol",
                 "2. 연구 목적\n일차 목적: 48주 EROA 변화량을 군간 비교한다. 이차 목적: GLS를 분석한다.\n"
                 "10. 심초음파 검사\n치료 층화를 위해 LVEF를 측정한다.\n"
                 f"11. 결과 측정 지표\n일차 평가변수: 기저 대비 48주 {outcome} 변화량(%p).\n"
                 f"12. 통계 분석\n일차 분석은 48주 {dependent}를 종속변수, 군과 기저 EROA를\n"
                 "설명변수로 한 ANCOVA를 적용한다. 이차 분석은 탐색적으로 해석한다.")

    findings, _ = run_rules([plan], None)

    endpoints = [issue for issue in findings if issue["code"] == "SEMANTIC_ENDPOINT"]
    assert bool(endpoints) is expected
    if expected:
        assert [issue["result"] for issue in endpoints] == ["needs_check"]
        quotes = [loc["quote"] for loc in endpoints[0]["locations"]]
        assert all(quote in plan.text for quote in quotes)
        assert any("일차 분석은 48주 EROA를 종속변수" in quote for quote in quotes)


@pytest.mark.parametrize(("disclosure", "record", "expected"), [
    ("", "48,000,000원", True),
    (", 가구소득", "48,000,000원", False),
    (", 본인 연소득, 가족 소득구간", "48,000,000원", True),
    ("", "미수집", False),
])
def test_flattened_crf_income_value_is_compared_to_the_collection_lists(
    disclosure: str, record: str, expected: bool,
) -> None:
    docs = [
        QCDoc("p", "연구계획서.pdf", "protocol",
              "15. 자료 보안 및 사생활 보호\n분석 자료는 연령, 성별, 병력, 약물,\n"
              "활력, 검사 결과, 이상사례, 방문·지급 여부 및 영상으로 한정한다."),
        QCDoc("c", "설명문.pdf", "consent_form",
              f"7. 개인정보 관리\n연구 자료에는 나이, 성별, 병력, 복용약{disclosure}, 혈\n"
              "압 등 활력, 검사 결과, 방문 및 지급 여부가 포함됩니다."),
        QCDoc("r", "증례기록서.pdf", "crf",
              "증례기록서: 등록 및 동의\n항목\n기록\n연령·성별\n62세 / 남성\n"
              f"가족 연소득\n{record}\n혈압·eGFR\n118/72 mmHg / 67 mL/min/1.73m²\n"),
    ]

    findings, _ = run_rules(docs, None)

    income = [issue for issue in findings if issue["code"] == "SEMANTIC_CRF_FIELDS"]
    assert bool(income) is expected
    if expected:
        assert [issue["result"] for issue in income] == ["needs_check"]
        locations = {loc["doc"]: loc["quote"] for loc in income[0]["locations"]}
        assert locations.keys() == {"p", "c", "r"}
        assert locations["r"] == "가족 연소득\n48,000,000원"
        assert all(locations[doc.key] in doc.text for doc in docs)
