import pytest

from irb_automation.qc.bundle import inspect_bundle
from irb_automation.qc.checks import QCDoc, check_version, run_rules
from irb_automation.qc.profile import build_profile
from irb_automation.submission.guess import guess_doc_type


@pytest.mark.parametrize(("case", "code"), [
    ("recruitment", "INTERVIEW_RECORDING_REQUIREMENT"),
    ("withdrawal", "INTERVIEW_WITHDRAWAL_WINDOW"),
    ("recontact", "INTERVIEW_COMPLETED_RECONTACT"),
    ("transcription", "INTERVIEW_EXTERNAL_TRANSCRIPTION"),
    ("retention", "INTERVIEW_AUDIO_RETENTION"),
    ("payment", "INTERVIEW_EARLY_STOP_PAYMENT"),
])
@pytest.mark.parametrize("corrected", [False, True])
def test_optional_recording_amendment_compares_the_current_procedures(case: str, code: str, corrected: bool) -> None:
    def choose(key: str, bad: str, good: str) -> str:
        return bad if key == case and not corrected else good

    docs = [
        QCDoc("a", "변경심의 신청서.txt", "application", "# 연구계획 변경심의 신청서"),
        QCDoc("p", "개정 연구계획서.txt", "protocol", "\n".join((
            "성인 개별 면담 연구에 선택적 녹음을 추가한다.",
            "녹음 미선택 또는 거절 시 필기 방식으로 진행한다.",
            "이미 면담을 마친 5명에게는 추가 연락을 하지 않는다.",
            "면담 전인 기존 동의자 3명은 녹음 추가선택서를 작성한다.",
            "담당자가 내부 단말에서 직접 전사한다. 외부 서비스로 원음을 보내지 않는다.",
            "원음은 검수 완료 즉시, 늦어도 면담 후 21일 내에 삭제한다.",
            "면담일 다음 날을 1일째로 계산하여 10일째까지 연구자료 삭제를 요청할 수 있다.",
            "연구 종료 후 3년간 가명 전사본을 보관한다.",
            "면담을 시작한 참여자는 완료·중단 여부와 관계없이 25,000원을 받는다.",
            "사례비 총 예산은 50만원이다.",
        ))),
        QCDoc("c", "설명문.txt", "consent_form",
              f"면담 다음 날부터 {choose('withdrawal', '4', '10')}일 이내에 자료 이용 철회를 요청할 수 있습니다.\n"
              "녹음 중단은 면담 중 즉시 요청할 수 있습니다."),
        QCDoc("s", "녹음 선택서.txt", "recording_choice",
              "면담 다음 날부터 10일 이내에 자료 이용 철회를 요청할 수 있습니다."),
        QCDoc("r", "모집공고문.txt", "recruitment", "\n".join((
            choose("recruitment", "음성 녹음에 동의하시는 분만 신청할 수 있습니다.", "녹음은 선택이며 거절해도 필기 방식으로 참여합니다."),
            "사례비 | 면담 시작 후 25,000원 지급. 중도 중단하셔도 전액을 드립니다.",
        ))),
        QCDoc("o", "면담 운영지침.txt", "questionnaire", "\n".join((
            "# 1 변경 시행 및 집단별 연락",
            choose("recontact", "완료자 5명에게 연락해 추가 방문을 제안한다.", "완료자에게 추가 연락이나 재면담을 하지 않는다."),
            "# 2 면담 질문지", "처음에는 무엇을 하셨나요?",
            "# 3 녹음·전사 및 폐기",
            choose("transcription", "외부 음성인식 서비스에 녹음 파일을 업로드해 전사한다.",
                   "기관 내부 단말에서 전사한다. 외부 전사 서비스로 음성을 보내지 않는다."),
            choose("retention", "원음은 면담일로부터 60일간 보관 후 삭제한다.",
                   "원음은 검수 완료 즉시, 늦어도 면담 후 21일 내에 삭제한다."),
            "# 4 시행 후 모의 예시", "과거 사례를 재구성한 음성 기록은 실제 등록에 합산하지 않는다.",
            "완료자 5명에게 연락해 추가 방문을 제안한다.",
            "# 중도 종료 시 지급 기록",
            f"면담 도중 그만둔 경우에는 상품권 {choose('payment', '12,500', '25,000')}원만 지급한다.",
        ))),
    ]

    issues, _ = run_rules(docs, None)

    found = [issue for issue in issues if issue["code"].startswith("INTERVIEW_")]
    assert {issue["code"] for issue in found} == (set() if corrected else {code})
    assert all(issue["code"] != "CROSS_EXPENSE" for issue in issues)
    for issue in found:
        texts = {doc.key: doc.text for doc in docs}
        assert "p" in {loc["doc"] for loc in issue["locations"]}
        assert all(loc["quote"] in texts[loc["doc"]] for loc in issue["locations"])
        if case == "payment":
            assert "12,500원" in issue["detail"] and "25,000원" in issue["suggestion"]


def test_amendment_bundle_uses_the_change_table_and_keeps_recording_choice_separate() -> None:
    plan = QCDoc("p", "개정 연구계획서.txt", "protocol", "기존 동의·면담 전 참여자는 추가선택서로 녹음을 선택한다.")
    application = QCDoc("a", "변경심의 신청서.txt", "application", "# 연구계획 변경심의 신청서")
    consent = QCDoc("c", "설명문 및 동의서.txt", "consent_form", "새 참여자 동의서")
    heading = "# 기존 참여자용 녹음 추가설명 및 선택서"
    name = "기존_참여자용_녹음_추가설명_및_선택서.docx"
    choice = QCDoc("s", name, guess_doc_type(name, extracted_text=heading).doc_type, heading)
    docs = [plan, application, consent, choice]

    profile = build_profile(None, None, docs)
    units, issues = inspect_bundle(docs, profile)

    assert profile["application"] == "amendment"
    assert choice.doc_type == "recording_choice"
    assert {check["field"] for check in units if check["result"] == "needs_check"} == {"amendment_table"}
    assert not any(issue["code"] == "BUNDLE_ROLE" for issue in issues)


@pytest.mark.parametrize(("filename", "expected"), [("비교표_v2.0.docx", set()), ("비교표_v3.0.docx", {"VERSION_MISMATCH"})])
def test_amendment_table_compares_its_current_version_and_keeps_prior_versions_as_history(
    filename: str, expected: set[str],
) -> None:
    doc = QCDoc("a", filename, "amendment_table",
                "개정 v2.0안\n# 변경 전후 대비표\n# 1 근거 문서와 적용 기준\n"
                "기존 v1.0 문서와 신청 v2.0안을 비교한다.\n변경 항목 | 기존 v1.0 | 신청 v2.0")

    assert {issue["code"] for issue in check_version(doc)} == expected
