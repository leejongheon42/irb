from __future__ import annotations

from datetime import date, timedelta

import pytest

from irb_automation.engine.checklist import build_checklist
from irb_automation.engine.prereview import run_prereview
from irb_automation.engine.ruleset import Ruleset, get_ruleset

from .conftest import good_facts


def codes(result):
    return {f["code"] for f in result["findings"]}


def test_pseudonymized_retrospective_is_exempt(settings):
    r = run_prereview(good_facts())
    assert r["review_type"] == "exempt"
    assert r["risk"]["level"] == "low"
    assert r["consent"]["status"] == "eligible"
    assert r["ready_for_review"] is True
    assert all(c["met"] for c in r["classification"]["exemption"]["criteria"])
    # 면제 시에는 심의의뢰서 대신 면제 확인 신청서가 요구된다
    docs = {c["doc"] for c in r["checklist"]}
    assert "exemption_request" in docs and "application" not in docs


def test_researcher_doing_own_pseudonymization_not_exempt(settings):
    r = run_prereview(good_facts(deid_performed_by="researcher"))
    assert r["review_type"] == "expedited"
    assert "DEID_SEPARATION" in codes(r)


def test_identifiable_with_vulnerable_and_sensitive_goes_full_board(settings):
    r = run_prereview(
        good_facts(
            deid_level="identifiable",
            identifiers_collected=["mrn", "name"],
            identifier_justification="추적 관찰 데이터 연계를 위해 등록번호와 성명이 반드시 필요함",
            vulnerable_populations=["children"],
            sensitive_categories=["mental_health"],
        )
    )
    assert r["review_type"] == "full"
    triggers = " ".join(r["classification"]["full_board_triggers"])
    assert "취약한 연구대상자" in triggers and "고민감정보" in triggers
    assert r["risk"]["level"] in ("moderate", "high")


def test_high_risk_ai_commercial_external(settings):
    r = run_prereview(
        good_facts(
            identifiers_collected=["mrn", "birth_date"],
            unstructured_data=["free_text"],
            sensitive_categories=["mental_health"],
            external_transfer=True,
            external_recipient="외부 기업",
            storage_location="external_cloud",
            ai_ml_development=True,
            commercial_use=True,
            expected_subjects=120_000,
        )
    )
    assert r["review_type"] == "full"
    assert r["risk"]["level"] == "high"
    assert {"FREE_TEXT", "EXTERNAL_STORAGE", "COMMERCIAL_USE", "AI_ML", "LARGE_DATASET"} <= codes(r)


def test_rrn_collection_is_critical(settings):
    r = run_prereview(good_facts(identifiers_collected=["rrn"], identifier_justification="본인 확인 목적의 연계가 필요함"))
    crit = [f for f in r["findings"] if f["severity"] == "critical"]
    assert any(f["code"] == "RRN_COLLECTION" for f in crit)
    assert "제24조의2" in next(f for f in crit if f["code"] == "RRN_COLLECTION")["legal"]
    assert r["ready_for_review"] is False


def test_anonymized_with_identifiers_is_inconsistent(settings):
    r = run_prereview(good_facts(deid_level="anonymized", identifiers_collected=["name"]))
    assert "DEID_INCONSISTENT" in codes(r)


def test_waiver_without_justification_is_blocking(settings):
    r = run_prereview(good_facts(waiver_impracticable_reasons=[], waiver_impracticable_detail=""))
    assert r["consent"]["status"] == "insufficient"
    assert any(f["code"] == "WAIVER_JUSTIFICATION" and f["severity"] == "critical" for f in r["findings"])


def test_waiver_identifiable_not_eligible(settings):
    r = run_prereview(good_facts(deid_level="identifiable", identifiers_collected=["mrn"],
                                 identifier_justification="연계를 위해 반드시 필요한 식별자임을 소명함"))
    assert r["consent"]["status"] == "not_eligible"


def test_consent_obtain_requires_consent_form(settings):
    r = run_prereview(good_facts(consent_plan="obtain"))
    missing = [c["doc"] for c in r["checklist"] if not c["provided"]]
    assert "consent_form" in missing
    assert r["consent"]["status"] == "not_applicable"


def test_missing_documents_are_critical(settings):
    r = run_prereview(good_facts(uploaded_doc_types=set()))
    assert r["summary"]["critical"] >= 5
    assert not r["ready_for_review"]


def test_future_data_period_flagged(settings):
    r = run_prereview(good_facts(data_period_end=date.today() + timedelta(days=30)))
    assert "DATA_PERIOD_FUTURE" in codes(r)


def test_expired_pi_training(settings):
    r = run_prereview(good_facts(pi_training_expires_on=date.today() - timedelta(days=1)))
    assert "PI_TRAINING_EXPIRED" in codes(r)


def test_international_and_linkage(settings):
    r = run_prereview(good_facts(international_transfer=True, data_linkage=True, linkage_target="건보공단"))
    assert {"INTERNATIONAL_TRANSFER", "DATA_LINKAGE"} <= codes(r)
    assert r["review_type"] != "exempt"


def test_continuing_review_only_needs_progress_report(settings):
    r = run_prereview(good_facts(submission_kind="continuing", previous_review_type="expedited",
                                 report={"progress_summary": "현재까지 자료 추출과 1차 분석을 완료하였고 결과를 정리 중임"}))
    assert r["review_type"] == "expedited"
    assert [c["doc"] for c in r["checklist"]] == ["progress_report"]


def test_final_report_with_breach_is_critical(settings):
    r = run_prereview(good_facts(submission_kind="final_report", report={"data_breach": True}))
    assert "DATA_BREACH" in codes(r)
    assert "DATA_NOT_DESTROYED" in codes(r)


def test_risk_factors_explain_score(settings):
    r = run_prereview(good_facts(rare_disease=True, ai_ml_development=True))
    assert r["risk"]["score"] == sum(f["points"] for f in r["risk"]["factors"])
    names = {f["factor"] for f in r["risk"]["factors"]}
    assert {"희귀질환(소수 집단)", "AI/ML 모델 개발", "식별가능성"} <= names


def test_unknown_checklist_condition_rejected(settings):
    base = get_ruleset()
    data = dict(base.data)
    data["required_documents"] = [{"doc": "protocol", "when": ["no_such_condition"]}]
    with pytest.raises(ValueError):
        build_checklist(good_facts(), "expedited", Ruleset(data, "x" * 64))


def test_ruleset_options_are_configurable(settings):
    base = get_ruleset()
    data = dict(base.data)
    review = dict(data["review_type"])
    exemption = dict(review["exemption"])
    exemption["allow_pseudonymized"] = False
    review["exemption"] = exemption
    data["review_type"] = review
    r = run_prereview(good_facts(), Ruleset(data, "y" * 64))
    assert r["review_type"] == "expedited"
