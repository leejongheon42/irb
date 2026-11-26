import pytest

from irb_automation.ai import client, tier
from irb_automation.ai.privacy import mask_text
from irb_automation.qc.ai_phase import run_ai
from irb_automation.qc.bundle import inspect_bundle
from irb_automation.qc.checks import QCDoc, run_rules
from irb_automation.qc.grouping import merge_ai_evidence
from irb_automation.qc.profile import build_profile
from irb_automation.submission.guess import guess_doc_type


@pytest.mark.parametrize(("case", "code"), [
    ("eligibility", "RETRO_ELIGIBILITY_PERIOD"),
    ("outcome", "RETRO_OUTCOME_WINDOW"),
    ("fields", "RETRO_UNDECLARED_FIELD"),
    ("access", "RETRO_LINK_KEY_ACCESS"),
    ("retention", "RETRO_ANALYSIS_RETENTION"),
    ("waiver", "RETRO_WAIVER_RATIONALE"),
])
@pytest.mark.parametrize("corrected", [False, True])
def test_retrospective_records_review_compares_the_same_research_scope(case: str, code: str, corrected: bool) -> None:
    def choose(key: str, error: str, valid: str) -> str:
        return error if case == key and not corrected else valid

    plan = QCDoc("p", "연구계획서.txt", "protocol", "\n".join((
        "후향적 의무기록 연구",
        "# 1 선정 기준",
        "2024.01.01부터 2024.12.31까지 퇴원한 환자를 대상으로 한다.",
        "# 2 결과의 정의",
        "일차 결과는 퇴원 다음 날부터 10일 이내 외래 방문의 유무이다.",
        "마지막 퇴원자는 2025.01.10까지 조회한다.",
        "# 3 수집 항목 및 자료원",
        "항목 | 자료원",
        "연령대, 성별 | 의무기록",
        "첫 방문 간격, 10일 방문 여부 | 방문 기록",
        "# 4 동의면제 요청",
        "연락처에는 보호자 번호가 섞여 있다. 개별 재접촉은 정보 노출 위험이 있다.",
        "# 5 자료 보관 및 폐기",
        "연결키는 기록관리자 김돌봄만 열 수 있고 2026.02.01 폐기한다. "
        "분석 파일은 연구 종료 후 4년간 보관하고 2030.09.30 폐기한다.",
    )))
    application = QCDoc("a", "신규심의 신청서.txt", "application",
                        "대상 퇴원 기간 | 2024-01-01 ~ " + choose("eligibility", "2025-01-10", "2024-12-31"))
    extraction = QCDoc("c", "자료추출 명세.txt", "crf", "\n".join((
        "# 1 추출 필드", "필드 | 기입 규칙", "연령대 / 성별 | 의무기록",
        f"첫 방문 간격 / 방문 여부 | 퇴원 다음 날부터 {choose('outcome', '20', '10')}일 이내 외래 방문.",
        choose("fields", "고용 상태 | 원기록에서 추출한다.", ""),
        "# 2 기입 예시", "연구번호 | 간격·여부",
        f"CASE1 | {choose('outcome', '15일·1', '—·0')}", "CASE2 | 10일·1", "CASE3 | ?·NA",
    )))
    management = QCDoc("m", "자료 접근 및 폐기 계획서.txt", "deid_plan", "\n".join((
        "연결키 | 2026.02.01 폐기한다.",
        choose("access", "연결키 | 김돌봄과 데이터 분석자에게 읽기 권한을 부여한다.",
               "연결키 | 김돌봄에게만 읽기 권한을 부여한다. 분석자에게 읽기 권한을 부여하지 않는다."),
        choose("retention", "가명 분석 파일 | 연구 종료 6년 후인 2032.09.30에 삭제한다.",
               "가명 분석 파일 | 연구 종료 4년 후인 2030.09.30에 삭제한다."),
    )))
    waiver = QCDoc("w", "동의면제 사유서.txt", "waiver_request", "\n".join((
        "# 동의면제 사유서", "후향적 기록 연구",
        "# 2 개별 동의를 받기 어려운 사유",
        choose("waiver", "연락에 드는 작업 시간을 줄일 필요가 있다.",
               "시간을 줄일 수 있고, 보호자 번호가 남아 재접촉 시 정보 노출 위험이 있다."),
        "# 3 위험 완화", "분석자에게 연결키를 전달하지 않는다.",
    )))
    docs = [plan, application, extraction, management, waiver]

    issues, _ = run_rules(docs, None)

    findings = [issue for issue in issues if issue["code"].startswith("RETRO_")]
    assert {issue["code"] for issue in findings} == (set() if corrected else {code})
    if not corrected:
        finding, = findings
        texts = {doc.key: doc.text for doc in docs}
        assert {loc["doc"] for loc in finding["locations"]} >= {"p", finding["doc"]}
        assert all(loc["quote"] in texts[loc["doc"]] for loc in finding["locations"])
        if case == "outcome":
            quotes = [loc["quote"] for loc in finding["locations"] if loc["doc"] == "c"]
            assert any("CASE1" in quote for quote in quotes)
            assert all("CASE2" not in quote and "CASE3" not in quote for quote in quotes)
        if case == "waiver":
            assert all(issue["code"] != "REQ_WAIVER_REQUEST_IMPRACTICABLE" for issue in issues)


@pytest.mark.parametrize(("filename", "heading", "expected"), [
    ("자료_접근_및_폐기_계획서.docx", "자료 접근 및 폐기 계획서", "deid_plan"),
    ("생명윤리_및_자료이용_준수서약서.docx", "생명윤리 및 자료이용 준수서약서", "pledge"),
    ("의무기록_이용_허가서.docx", "의무기록 이용 허가서", "records_access_evidence"),
    ("연구계획서.docx", "연구계획서", "protocol"),
])
def test_retrospective_document_titles_classify_before_generic_terms(filename: str, heading: str, expected: str) -> None:
    assert guess_doc_type(filename, extracted_text=f"# {heading}").doc_type == expected


@pytest.mark.parametrize("evidence_uploaded", [False, True])
def test_new_internal_records_bundle_checks_only_its_applicable_documents(evidence_uploaded: bool) -> None:
    docs = [
        QCDoc("p", "계획서.txt", "protocol", "기관 자료이용 허가 후 기존 기록을 추출한다."),
        QCDoc("a", "신규심의_신청서.txt", "application", "기존 의무기록을 이용한 단일기관 후향적 인간대상연구"),
        QCDoc("w", "동의면제 사유서", "waiver_request", "서면동의면제 신청"),
    ]
    if evidence_uploaded:
        docs.extend((
            QCDoc("e", "기관 자료이용 허가서", "records_access_evidence", "관리부서에서 내부 이용을 허가한다."),
            QCDoc("c", "이해상충 공개서", "coi", "이해상충 공개"),
            QCDoc("t", "교육 이수증", "training_cert", "교육 이수"),
        ))

    profile = build_profile(None, None, docs)
    checks, _ = inspect_bundle(docs, profile)

    assert {check["field"]: check["reason"] for check in checks if check["result"] == "needs_check"} == (
        {} if evidence_uploaded else {
            "training_cert": "file_missing", "coi": "submission_unverified", "records_access_evidence": "file_missing",
        }
    )


@pytest.mark.parametrize(("role", "cover"), [
    ("protocol", "연구계획서"),
    ("application", "신규심의 신청서"),
])
@pytest.mark.parametrize("title", ["퇴원 후 외래 방문에 영향을 주는 요인 분석", "연구 과제명 | [작성]", ""])
def test_cover_heading_with_an_actual_study_title_satisfies_the_title_requirement(
    role: str, cover: str, title: str,
) -> None:
    doc = QCDoc("p", "제출서류.docx", role, "\n".join((
        "ABC-RC-10 / v1.0 / 2026.11.03",
        f"# {cover}", title, "연구책임자 홍연구 | 모의의료원",
        "# 1 연구 목적", "퇴원 후 외래 방문 여부를 조사한다.",
        "# 2 연구 방법", "과거 진료기록을 이용한다.",
    )))

    issues, _ = run_rules([doc], None)

    assert any(issue["code"] == f"REQ_{role.upper()}_TITLE" for issue in issues) == (not title or "[작성]" in title)


@pytest.mark.parametrize("sentence", [
    "만 65세 이상 환자를 대상으로 한다.",
    "예약을 잡아 주어도 환자가 방문했는지 확인한다.",
])
def test_patient_name_masking_preserves_age_and_narrative_qualifiers(sentence: str) -> None:
    guarded = mask_text(f"{sentence} 홍길동 환자는 연락했다.")
    assert guarded.text == f"{sentence} [이름] 환자는 연락했다."
    assert guarded.pii == {"name": 1}


@pytest.mark.parametrize(("rule_id", "flag"), [
    ("M10", "서술 모순"), ("E05", "수행 절차·검체"),
])
def test_paid_focus_review_does_not_repeat_the_same_outcome_window_finding(
    monkeypatch: pytest.MonkeyPatch, rule_id: str, flag: str,
) -> None:
    plan = "일차 결과는 퇴원 다음 날부터 14일 이내 외래 방문의 유무이다."
    spec = "첫 방문 간격 / 방문 여부 | 퇴원 다음 날부터 30일 이내 외래 방문."
    docs = [
        QCDoc("p", "계획서.txt", "protocol", f"후향적 의무기록 연구\n# 1 일차 결과의 정의\n{plan}"),
        QCDoc("r", "증례기록서.txt", "crf", f"# 1 추출 필드\n{spec}"),
    ]
    from irb_automation.qc.units import annotate

    found, _ = run_rules(docs, None)
    known = [annotate(issue) for issue in found if issue["code"] == "RETRO_OUTCOME_WINDOW"]
    assert len(known) == 1
    monkeypatch.setattr(client, "ai_enabled", lambda: True)

    def respond(*, purpose: str, **kwargs) -> dict:
        if purpose == "서류 품질 점검":
            return {"summary": "", "issues": []}
        return {"summary": "", "issues": [{
            "doc_index": 1, "severity": "major", "category": "cross", "rule_id": rule_id, "flag": flag,
            "title": "일차 결과 관찰기간 불일치",
            "locations": [{"doc_index": 0, "quote": plan}, {"doc_index": 1, "quote": spec}],
        }]}

    monkeypatch.setattr(client, "structured_call", respond)
    with tier.use(tier.CLAUDE):
        issues, _ = run_ai(docs, known)

    assert merge_ai_evidence(known, issues) == []
    if rule_id == "E05":
        assert {location["doc"] for location in known[0]["ai_evidence"]} == {"p", "r"}
