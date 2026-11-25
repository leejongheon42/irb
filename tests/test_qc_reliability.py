"""Regression cases from the document review improvement plan."""
from __future__ import annotations

import pytest

from irb_automation.qc import checks, facts
from irb_automation.qc.scoring import assess


@pytest.mark.parametrize(("text", "key", "value"), [
    ("본 연구에는 120명이 참여합니다.", "subjects", "120"),
    ("시험군 60명, 대조군 60명", "subjects", "120"),
    ("방문은 여덟 차례", "visits", "8"),
    ("매일 아침 100mg", "dosage", "1회/100mg"),
    ("자료는 3년이 지나면 폐기합니다.", "retention", "3"),
])
def test_natural_language_facts(text: str, key: str, value: str) -> None:
    extracted = facts.extract(text)
    assert [(f.key, f.value) for f in extracted] == [(key, value)]
    assert all(f.raw in text for f in extracted)


def test_group_counts_are_not_corrected_as_a_total() -> None:
    original = "시험군 60명, 대조군 60명"
    assert facts.restyle("subjects", original, facts.Fact("subjects", "140", "")) is None


@pytest.mark.parametrize("ending", ["", "."])
def test_keywords_alone_do_not_satisfy_consent_requirements(ending: str) -> None:
    text = "목적 절차 참여 기간 120명 위험 이익 개인정보 자발 철회 불이익 보상 비용 연구책임자 IRB 열람 보관 새로운 정보 서명 날짜" + ending
    issues = checks.check_required(checks.QCDoc("d1", "동의서.txt", "consent_form", text), set())
    assert {i["code"] for i in issues} >= {
        "REQ_CONSENT_FORM_PURPOSE", "REQ_CONSENT_FORM_RISK", "REQ_CONSENT_FORM_VOLUNTARY",
        "REQ_CONSENT_FORM_NO_PENALTY", "REQ_CONSENT_FORM_IRB_CONTACT",
    }


def test_unverified_ai_does_not_affect_score() -> None:
    result = assess([{"severity": "critical", "confidence": "ai_unverified", "review": "confirmed"}])
    assert result["score"] == 100 and result["status"] == "HOLD"
    assert result["counts"] == {"critical": 0, "major": 0, "minor": 0}


def test_negated_withdrawal_right_is_not_satisfied() -> None:
    text = "참여는 자발적으로 결정하지만 연구 시작 후에는 철회할 수 없습니다."
    issues = checks.check_required(checks.QCDoc("d1", "동의서.txt", "consent_form", text), set())
    assert any(i["code"] == "REQ_CONSENT_FORM_VOLUNTARY" for i in issues)


@pytest.mark.parametrize("text", [
    "자발적으로 참여하며 언제든 연구책임자에게 연락하여 참여를 철회할 수 있습니다.",
    "1. 자발적 참여\n언제든 담당 연구자에게 말씀하여 참여를 중단할 수 있습니다.",
    "1. 자발적 참여\n언제든 참여를 철회할 수 있습니다.\n\n담당 연구자에게 연락하여 철회를 요청하십시오.",
])
def test_complete_withdrawal_right_is_satisfied(text: str) -> None:
    issues = checks.check_required(checks.QCDoc("d1", "동의서.txt", "consent_form", text), set())
    assert not any(i["code"] == "REQ_CONSENT_FORM_VOLUNTARY" for i in issues)


@pytest.mark.parametrize("quote, doc_index, verified", [
    ("이 연구의 위험은 두통입니다.", 0, True),
    ("이 연구의 위험은\n두통입니다.", 0, True),
    ("이 연구는 사망 위험이 있습니다.", 0, False),
    ("", 0, False),
    ("이 연구의 위험은 두통입니다.", 7, True),  # 문서 번호가 틀려도 원문에 있는 인용이면 검증된 것으로 본다
])
def test_ai_evidence_keeps_findings_as_candidates(monkeypatch: pytest.MonkeyPatch, quote: str, doc_index: int, verified: bool) -> None:
    from irb_automation.ai import client
    from irb_automation.qc.ai_phase import run_ai

    monkeypatch.setattr(client, "ai_enabled", lambda: True)
    monkeypatch.setattr(client, "structured_call", lambda **kwargs: {
        "summary": "", "issues": [{
            "doc_index": doc_index, "severity": "critical", "category": "context", "title": "위험 확인",
            "evidence": quote, "fix_old": "두통", "fix_new": "어지럼증",
        }],
    })
    doc = checks.QCDoc("d1", "동의서.txt", "consent_form", "이 연구의 위험은 두통입니다.")

    issues, _ = run_ai([doc], [])

    assert issues[0]["confidence"] == ("ai_verified" if verified else "ai_unverified")
    assert issues[0]["fixes"] == []
    assert assess(issues)["score"] == 100
    assert assess(issues)["status"] == "HOLD"


def test_extraction_benchmark_regression_floor() -> None:
    from irb_automation.qc.eval import evaluate

    report = evaluate()
    assert report["recall"] >= 0.9
    assert report["false_positives"] == 0
    assert report["reproducibility"] == 1


def test_structured_consent_fields_do_not_require_narrative() -> None:
    text = "참여 기간: 6개월\n대상자 수: 120명\n연구자 연락처: 02-1234-5678\nIRB: 02-1234-9999\n연구대상자 서명: ______\n날짜: ______"
    issues = checks.check_required(checks.QCDoc("d1", "동의서.txt", "consent_form", text), set())
    assert not {i["code"] for i in issues} & {
        "REQ_CONSENT_FORM_DURATION", "REQ_CONSENT_FORM_SUBJECTS", "REQ_CONSENT_FORM_RESEARCHER_CONTACT",
        "REQ_CONSENT_FORM_IRB_CONTACT", "REQ_CONSENT_FORM_SIGNATURE",
    }


def test_ai_quotes_packed_into_evidence_are_split_and_located(monkeypatch: pytest.MonkeyPatch) -> None:
    """Weaker models leave locations empty, number documents from 1 and pick an unrelated flag."""
    from irb_automation.ai import client
    from irb_automation.qc.ai_phase import run_ai

    docs = [checks.QCDoc("c", "동의서.txt", "consent_form", "수집된 자료는 3년간 보관 후 폐기합니다."),
            checks.QCDoc("p", "계획서.txt", "protocol", "연구 종료 후 5년간 보관하고 이후 파기한다.")]
    monkeypatch.setattr(client, "ai_enabled", lambda: True)
    monkeypatch.setattr(client, "structured_call", lambda **kwargs: {"summary": "", "issues": [
        {"doc_index": 2, "severity": "major", "category": "cross", "title": "보관 기간 불일치",
         "rule_id": "M10", "flag": "동일 개념 명칭 혼용", "locations": [],
         "evidence": '[문서 1] "수집된 자료는 3년간 보관 후 폐기합니다." / [문서 2] "연구 종료 후 5년간 보관하고 이후 파기한다."'},
        {"doc_index": 0, "severity": "major", "category": "cross", "title": "한쪽 인용이 원문에 없음", "locations": [],
         "evidence": '"수집된 자료는 3년간 보관 후 폐기합니다." / "연구 종료 후 10년간 보관한다."'},
    ]})

    found, missing = run_ai(docs, [])[0]

    assert found["confidence"] == "ai_verified" and found["result"] == "mismatch"
    assert [loc["doc"] for loc in found["locations"]] == ["c", "p"] and found["doc"] == "c"
    assert (found["rule_id"], found["flags"]) == ("M10", ["서술 모순"])
    assert missing["confidence"] == "ai_unverified" and missing["result"] == "needs_check"


@pytest.mark.parametrize(("body", "accepted"), [
    ("예상되는 이상반응으로는 두통, 오심 등이 있으며 즉시 연구자에게 알려 주십시오.", True),
    ("연구 참여 전 대상자에게 서면 동의를 받는다.", True),
    ("두통, 오심, 발진, 어지럼증, 저혈당, 피로감", False),
    ("받는다.", False),
])
def test_one_complete_sentence_counts_as_written_content(body: str, accepted: bool) -> None:
    from irb_automation.qc.structure import substantive

    assert substantive(body, 15) is accepted


def test_no_penalty_sentence_is_not_read_as_denied_withdrawal() -> None:
    from irb_automation.qc.required import check_required

    text = ("# 자발적 참여와 철회\n귀하의 참여는 전적으로 자발적이며 언제든지 참여를 철회할 수 있습니다. "
            "철회하려면 연구책임자에게 연락해 요청하시면 됩니다. 참여를 거부하거나 철회해도 불이익이 없습니다.\n")
    codes = {issue["code"] for issue in check_required(checks.QCDoc("d1", "동의서.txt", "consent_form", text), set())}
    assert "REQ_CONSENT_FORM_VOLUNTARY" not in codes
    denied = text.replace("언제든지 참여를 철회할 수 있습니다", "언제든지 문의할 수 있으나 철회는 불가합니다")
    codes = {issue["code"] for issue in check_required(checks.QCDoc("d1", "동의서.txt", "consent_form", denied), set())}
    assert "REQ_CONSENT_FORM_VOLUNTARY" in codes


def test_single_document_ai_finding_tagged_cross_is_recategorised_and_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    from irb_automation.ai import client
    from irb_automation.qc.ai_phase import run_ai
    from irb_automation.qc.grouping import merge_ai_evidence

    doc = checks.QCDoc("d1", "동의서.txt", "consent_form", "귀하는 본 연구에 연구대상지로 참여합니다. 피험자 정보는 보호됩니다.")
    monkeypatch.setattr(client, "ai_enabled", lambda: True)
    monkeypatch.setattr(client, "structured_call", lambda **kwargs: {"summary": "", "issues": [
        {"doc_index": 0, "severity": "minor", "category": "cross", "title": "오탈자 '연구대상지'", "rule_id": "M08",
         "flag": "의학용어 철자 오류", "locations": [{"doc_index": 0, "quote": "연구대상지로 참여합니다"}]},
        {"doc_index": 0, "severity": "major", "category": "cross", "title": "보호 방법 설명 없음", "rule_id": "M10",
         "flag": "서술 모순", "locations": [{"doc_index": 0, "quote": "피험자 정보는 보호됩니다."}]},
    ]})
    known = [{"code": "TYPO", "doc": "d1", "doc_name": "동의서.txt", "severity": "minor", "category": "typo", "title": "오탈자: '연구대상지' → '연구대상자'", "locations": []}]

    issues, _ = run_ai([doc], known)

    assert [(i["category"], i["confidence"]) for i in issues] == [("typo", "ai_verified"), ("context", "ai_verified")]
    kept = merge_ai_evidence(known, issues)
    assert [i["title"] for i in kept] == ["보호 방법 설명 없음"] and known[0]["ai_evidence"]


def test_short_noun_lines_and_table_rows_stay_in_their_marked_section() -> None:
    """제목 표시가 있는 문서에서 명사형 근거 문장·변수 표를 제목으로 오인해 '내용 미흡'을 내지 않는다."""
    from irb_automation.qc.required import check_required

    text = ("# 1. 연구 목적\n위험을 평가한다.\n# 2. 연구 배경\n실제 진료 자료 근거는 부족하다.\n"
            "# 5. 연구대상자 수 및 산출 근거\n목표 연구대상자 수: 총 1,500명\n"
            "연구 기간 내 적격 환자 전수, 사건 발생률 8% 가정 시 검정력 85%\n"
            "# 7. 수집 자료(변수) 항목 및 자료원\n자료원: 전자의무기록(EMR)\n"
            "수집 항목 | 구분 | 처리 방법\n연령 | 준식별자 | 5세 구간 범주화\n성별 | 준식별자 | 유지\n")
    codes = {i["code"] for i in check_required(checks.QCDoc("p", "계획서.docx", "protocol", text), set())}

    assert not codes & {"REQ_PROTOCOL_SAMPLE_SIZE", "REQ_PROTOCOL_VARIABLES"}


def test_heading_only_required_section_is_still_reported() -> None:
    from irb_automation.qc.required import check_required

    text = ("# 1. 연구 목적\n위험을 평가한다.\n# 2. 연구 배경\n근거는 부족하다.\n"
            "# 5. 연구대상자 수 및 산출 근거\n해당 없음\n# 7. 수집 자료(변수) 항목\n추후 기재\n")
    found = {i["code"]: i for i in check_required(checks.QCDoc("p", "계획서.docx", "protocol", text), set())}

    assert {"REQ_PROTOCOL_SAMPLE_SIZE", "REQ_PROTOCOL_VARIABLES"} <= set(found)
    assert found["REQ_PROTOCOL_VARIABLES"]["section"].startswith("# 7.")


@pytest.mark.parametrize(("body", "missing"), [
    ("만 19세 이상이며 두 번 방문할 수 있는 성인이다. 연구진의 감독을 받는 직원은 모집하지 않는다.", False),
    ("만 19세 이상이며 두 번 방문할 수 있는 성인이다.", True),
    ("추후 기재", True),
])
def test_population_section_checks_eligibility_and_exclusion_in_prose(body: str, missing: bool) -> None:
    from irb_automation.qc.required import check_required

    text = f"# 1. 연구 목적\n업무 피로를 조사한다.\n# 7. 연구대상자\n{body}\n# 8. 산출 근거\n목표 규모는 80명이다."
    findings = check_required(checks.QCDoc("p", "계획서.docx", "protocol", text), set())
    assert any(issue["code"] == "REQ_PROTOCOL_POPULATION" for issue in findings) == missing


def test_visit_comparison_uses_totals_and_still_detects_a_real_mismatch() -> None:
    from irb_automation.qc.facts import extract

    consent = "첫 방문 1회와 28일 후 추적 방문 1회에 각각 설문을 작성합니다."
    assert not any(fact.key == "visits" for fact in extract(consent))
    docs = [checks.QCDoc("p", "계획서.txt", "protocol", "총 2회 방문하며 결과를 기록한다."),
            checks.QCDoc("c", "동의서.txt", "consent_form", f"{consent}\n전체 3회 방문합니다.")]
    findings, _ = checks.run_rules(docs, None)
    cross, = [issue for issue in findings if issue["code"] == "CROSS_VISITS"]
    assert {loc["norm"] for loc in cross["locations"]} == {"2", "3"}


def _verify(monkeypatch: pytest.MonkeyPatch, issues: list[dict], reviews: list[dict], text: str) -> dict:
    from irb_automation.ai import client
    from irb_automation.qc.ai_verify import verify_rule_findings

    monkeypatch.setattr(client, "ai_enabled", lambda: True)
    monkeypatch.setattr(client, "structured_call", lambda **kwargs: {"reviews": reviews})
    return verify_rule_findings([checks.QCDoc("p", "계획서.docx", "protocol", text)], issues)


def _rule_issue(severity: str = "major") -> dict:
    return {"severity": severity, "category": "required", "title": "필수 기재사항 내용 미흡: 대상자 수 및 산출 근거",
            "detail": "", "doc": "p", "doc_name": "계획서.docx", "evidence": "", "confidence": "rule",
            "result": "missing", "review": "candidate", "fixes": []}


def test_ai_recheck_withdraws_rule_finding_only_with_a_quote_from_the_document(monkeypatch: pytest.MonkeyPatch) -> None:
    from irb_automation.qc.scoring import assess

    text = "# 5. 대상자 수\n목표 연구대상자 수: 총 1,500명, 검정력 85%"
    quoted, invented, agreed = _rule_issue(), _rule_issue(), _rule_issue()
    info = _verify(monkeypatch, [quoted, invented, agreed], [
        {"index": 0, "verdict": "false_positive", "reason": "수와 근거가 적혀 있음", "quote": "총 1,500명, 검정력 85%"},
        {"index": 1, "verdict": "false_positive", "reason": "적혀 있음", "quote": "총 2,000명을 모집한다"},
        {"index": 2, "verdict": "valid", "reason": "근거가 없음", "quote": ""},
    ], text)

    assert (info["status"], info["dismissed"]) == ("done", 1)
    assert quoted["review"] == "dismissed" and quoted["review_note"].startswith("AI 재검토")
    assert invented["review"] == "candidate" and invented["ai_review"]["verdict"] == "uncertain"
    assert agreed["review"] == "candidate" and agreed["ai_review"]["verdict"] == "valid"
    assert assess([quoted])["projected"] == 100 and assess([invented])["projected"] == 93


def test_ai_recheck_passes_cited_critical_finding_without_manual_review(monkeypatch: pytest.MonkeyPatch) -> None:
    from irb_automation.qc.category_scores import score_run
    from irb_automation.qc.models import QCRun

    issue = _rule_issue("critical")
    issue.update(id=1, result="needs_check", reason="basis_unverified", question="원문을 확인해 주세요.")
    info = _verify(monkeypatch, [issue], [
        {"index": 0, "verdict": "false_positive", "reason": "적혀 있음", "quote": "총 1,500명, 검정력 85%"}],
        "목표 연구대상자 수: 총 1,500명, 검정력 85%")

    assert (info["dismissed"], info["held"]) == (1, 0)
    assert (issue["review"], issue["result"]) == ("dismissed", "match")
    assert not issue.get("question")
    run = QCRun(issues=[issue], docs=[{"key": "p", "chars": 32}])
    score_run(run)
    assert (run.verdict, run.score, run.counts["notes"]) == ("PASS", 100, [])


def test_ai_recheck_failure_leaves_rule_findings_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    from irb_automation.ai import client
    from irb_automation.qc.ai_verify import verify_rule_findings

    def unavailable(**kwargs):
        raise client.AIError("연결 실패")

    issue = _rule_issue()
    monkeypatch.setattr(client, "ai_enabled", lambda: True)
    monkeypatch.setattr(client, "structured_call", unavailable)
    info = verify_rule_findings([checks.QCDoc("p", "계획서.docx", "protocol", "본문 내용입니다.")], [issue])

    assert info["status"] == "failed" and issue == _rule_issue()


def test_ai_recheck_citations_must_cover_the_documents_in_the_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    from irb_automation.ai import client
    from irb_automation.qc.ai_verify import verify_rule_findings

    docs = [checks.QCDoc("p", "계획서.txt", "protocol", "목표 인원은 총 80명입니다."),
            checks.QCDoc("a", "신청서.txt", "application", "최종적으로 분석 가능한 인원은 64명입니다.")]
    wrong_doc, partial, complete = _rule_issue(), _rule_issue(), _rule_issue()
    for issue in (partial, complete):
        issue.update(doc=None, category="cross", locations=[{"doc": doc.key} for doc in docs])
    first, second = ({"doc_index": n, "quote": doc.text} for n, doc in enumerate(docs))
    rows = [{"index": n, "verdict": "false_positive", "reason": "비교한 수치의 범위가 다릅니다.", "locations": locations}
            for n, locations in enumerate(([second], [first], [first, second]))]
    monkeypatch.setattr(client, "ai_enabled", lambda: True)
    monkeypatch.setattr(client, "structured_call", lambda **kwargs: {"reviews": rows})

    info = verify_rule_findings(docs, [wrong_doc, partial, complete])

    assert info["dismissed"] == 1 and info["reviewed"] == 3
    assert wrong_doc["review"] == partial["review"] == "candidate"
    assert complete["review"] == "dismissed"
    assert {location["doc"] for location in complete["ai_review"]["locations"]} == {"p", "a"}
