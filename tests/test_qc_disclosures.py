from __future__ import annotations

import pytest

from irb_automation.ai import client, tier
from irb_automation.ai.privacy import mask_text
from irb_automation.qc.ai_phase import run_ai
from irb_automation.qc.ai_verify import verify_rule_findings
from irb_automation.qc.checks import QCDoc, check_required, run_rules
from irb_automation.qc.units import annotate
from irb_automation.submission.guess import guess_doc_type


def test_specimen_consent_is_classified_by_its_official_form_header() -> None:
    guess = guess_doc_type('혈액 제공 동의서.pdf', extracted_text='[별지 제34호서식]\n인체유래물 연구 동의서')

    assert guess.doc_type == 'specimen_consent'


@pytest.mark.parametrize(('repair', 'missing'), [
    ('', {'IRB_CONTACT', 'COMPENSATION'}),
    ('IRB 문의: 02-123-4567\n', {'COMPENSATION'}),
    ('연구로 인한 손상이 생기면 연구기관이 치료비를 부담하고 보상을 신청할 수 있습니다.\n', {'IRB_CONTACT'}),
])
def test_consent_disclosures_require_the_right_contact_and_type_of_compensation(
    repair: str, missing: set[str],
) -> None:
    text = (
        'IRB의 승인을 받은 뒤 진행됩니다. 연구책임자에게 문의하십시오. 전화: 02-321-7654\n'
        '연구 중 이상반응이 나타나면 의료진이 필요한 진료를 제공합니다.\n'
        '참여 보상으로 교통비를 지급합니다.\n' + repair
    )

    issues = {i['code'].removeprefix('REQ_CONSENT_FORM_'): i
              for i in check_required(QCDoc('c', '참여동의서.txt', 'consent_form', text), set())}

    assert issues.keys() & {'IRB_CONTACT', 'COMPENSATION'} == missing
    if 'IRB_CONTACT' in missing:
        assert '연구책임자에게 문의' in issues['IRB_CONTACT']['evidence']
    if 'COMPENSATION' in missing:
        assert '필요한 진료를 제공합니다' in issues['COMPENSATION']['evidence']


@pytest.mark.parametrize('rights_contact', ['', '대상자 권리 문의: 독립 IRB, rights@example.invalid\n'])
def test_research_inquiries_are_separate_from_independent_irb_contact(rights_contact: str) -> None:
    research_contact = '연구 관련 문의: 담당자, research@example.invalid (가상 연락처).'
    text = ('9. 연구 문의\n' + research_contact + '\n' + rights_contact + '10. 동의 확인\n')

    issues = {issue['code']: issue for issue in check_required(QCDoc('c', '동의서.pdf', 'consent_form', text), set())}

    assert 'REQ_CONSENT_FORM_RESEARCHER_CONTACT' not in issues
    if rights_contact:
        assert 'REQ_CONSENT_FORM_IRB_CONTACT' not in issues
    else:
        assert issues['REQ_CONSENT_FORM_IRB_CONTACT']['evidence'] == research_contact


@pytest.mark.parametrize(('changed', 'expected'), [(False, {}), (True, {
    ('CROSS_SAMPLE_VOLUME', '채혈'): {'10mL', '15mL'},
    ('CROSS_SAMPLE_VOLUME', '전체 채혈'): {'1000mL', '1500mL'},
    ('CROSS_EXPENSE', '참여 지급액'): {'20000', '30000'},
})])
def test_bundle_compares_collection_and_payment_in_prose_and_tables(changed: bool, expected: dict) -> None:
    quantity, total, payment = ('15', '1,500', '3') if changed else ('10', '1,000', '2')
    docs = [
        QCDoc('p', '계획서.pdf', 'protocol', '정맥혈 10 mL를 1회 채취한다.\n'
              '총 예정 채취량은 10 mL × 100명 = 1,000 mL이다.\n혈청은 0.5 mL씩 분주한다.\n'
              '방문에 따른 교통비 명목으로 1인당 20,000원을 지급한다.'),
        QCDoc('r', '증례기록서.docx', 'crf', '정맥혈 채취 | 완료 / 10 mL\n'
              '방문 교통비 지급 | 지급 / 2027.02.15 / 20,000원'),
        QCDoc('n', '모집공고문.docx', 'recruitment', f'혈액 채취(10mL)를 진행합니다. 방문 교통비로 {payment}만원을 지급합니다.'),
        QCDoc('c', '참여동의서.pdf', 'consent_form', f'혈액 {quantity}mL를 한 번 채취합니다. '
              f'총 100명으로부터 {total}mL를 수집할 예정입니다. 방문에 대한 사례비로 2만원을 지급합니다.'),
        QCDoc('s', '검체동의서.pdf', 'specimen_consent', '정맥혈 10 mL (1회)'),
    ]

    findings, _ = run_rules(docs, None)

    observed = {(i['code'], i['scope']): {loc['norm'] for loc in i['locations']}
                for i in findings if i['code'] in {'CROSS_SAMPLE_VOLUME', 'CROSS_EXPENSE'} and i['result'] == 'mismatch'}
    assert observed == expected
    if changed:
        collection = next(i for i in findings if i.get('scope') == '채혈')
        assert {loc['doc'] for loc in collection['locations']} == {'p', 'r', 'n', 'c', 's'}


def test_paid_review_recovers_semantic_findings_when_general_review_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    objective = "이차 목적은 검사 A의 군 간 차이 평가입니다."
    endpoint = "이차 평가변수는 검사 B의 결과입니다."
    listed = "수집 자료는 연령과 성별입니다."
    extra = "운동 습관 | 주 2회"
    docs = [
        QCDoc("p", "계획서.txt", "protocol", f"{objective}\n{endpoint}\n{listed}"),
        QCDoc("r", "기록서.txt", "crf", extra),
    ]
    monkeypatch.setattr(client, "ai_enabled", lambda: True)

    def respond(*, purpose: str, **kwargs) -> dict:
        if purpose != "연구계획·수집항목 집중 점검":
            return {"summary": "", "issues": []}
        return {"summary": "", "issues": [
            {"doc_index": 0, "severity": "major", "category": "context", "rule_id": "M10", "flag": "서술 모순",
             "title": "이차 목적과 평가변수 불일치",
             "locations": [{"doc_index": 0, "quote": objective}, {"doc_index": 0, "quote": endpoint}]},
            {"doc_index": 1, "severity": "major", "category": "cross", "rule_id": "E06", "flag": "개인정보·이용·제공",
             "title": "수집 항목 불일치",
             "locations": [{"doc_index": 0, "quote": listed}, {"doc_index": 1, "quote": extra}]},
            {"doc_index": 1, "severity": "major", "category": "cross", "rule_id": "E06", "flag": "개인정보·이용·제공",
             "title": "한쪽 문서 인용이 빠진 수집 항목 지적",
             "locations": [{"doc_index": 1, "quote": extra}]},
        ]}

    monkeypatch.setattr(client, "structured_call", respond)

    with tier.use(tier.CLAUDE):
        findings, info = run_ai(docs, [])

    assert info["status"] == "done"
    assert {issue["rule_id"]: {loc["doc"] for loc in issue["locations"]}
            for issue in findings if issue["confidence"] == "ai_verified"} == {"M10": {"p"}, "E06": {"p", "r"}}
    assert [issue["rule_id"] for issue in findings if issue["result"] == "needs_check"] == ["E06"]


@pytest.mark.parametrize("corrected", [False, True])
def test_explicit_endpoint_and_crf_field_differences_are_visible_without_provider(corrected: bool) -> None:
    endpoint = "TSH" if corrected else "T3"
    extra = ", 운동 습관" if corrected else ""
    docs = [
        QCDoc("p", "계획서.pdf", "protocol",
              "이차 목적은 T\nSH의 군 간 차이를 평가하는 것이다.\n"
              "혈청 TSH를 분석 장비로 정량한다.\n"
              f"이차 평가변수는 {endpoint} 농도이다.\n"
              f"임상정보: 연령, 성별, BMI{extra}."),
        QCDoc("c", "설명문.pdf", "consent_form", f"수집 정보는 연령, 성별, BMI{extra}입니다."),
        QCDoc("r", "증례기록서.docx", "crf", "2. 임상정보\n항목 | 값\n성별 | 기록함\n"
              "BMI (kg/m²) | 25\n운동 습관 | 주 2회\n3. 자격 확인\n임신 여부 | 해당 없음"),
    ]

    findings, _ = run_rules(docs, None)

    semantic = {i["code"]: i for i in findings if i["code"].startswith("SEMANTIC_")}
    assert set(semantic) == (set() if corrected else {"SEMANTIC_ENDPOINT", "SEMANTIC_CRF_FIELDS"})
    if not corrected:
        assert {loc["doc"] for loc in semantic["SEMANTIC_CRF_FIELDS"]["locations"]} == {"p", "c", "r"}
        assert "임신 여부" not in semantic["SEMANTIC_CRF_FIELDS"]["detail"]
        assert all(loc["quote"] in next(doc.text for doc in docs if doc.key == loc["doc"])
                   for issue in semantic.values() for loc in issue["locations"])


def test_final_review_keeps_disclosures_when_the_cited_text_covers_a_different_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contact = "연구책임자 연락처: 02-123-4567"
    response = "이상반응이 발생하면 의료진이 필요한 진료를 제공합니다."
    doc = QCDoc("c", "동의서.txt", "consent_form", f"{contact}\n{response}")
    by_code = {i["code"]: annotate(i) for i in check_required(doc, set())}
    issues = [by_code["REQ_CONSENT_FORM_IRB_CONTACT"], by_code["REQ_CONSENT_FORM_COMPENSATION"]]
    monkeypatch.setattr(client, "ai_enabled", lambda: True)
    monkeypatch.setattr(client, "structured_call", lambda **kwargs: {"reviews": [
        {"index": index, "verdict": "false_positive", "reason": "문서에 관련 안내가 있습니다.",
         "locations": [{"doc_index": 0, "quote": mask_text(quote).text}]}
        for index, quote in enumerate((contact, response))
    ]})

    verify_rule_findings([doc], issues)

    assert [(issue["review"], issue["ai_review"]["verdict"]) for issue in issues] == [
        ("candidate", "uncertain"), ("candidate", "uncertain"),
    ]


def test_masking_keeps_won_amounts_available_for_comparison() -> None:
    guarded = mask_text("교통비로 4만원을 지급합니다. 사례비로 40,000원을 지급합니다. 문의: 02-123-4567")

    assert guarded.text == "교통비로 4만원을 지급합니다. 사례비로 40,000원을 지급합니다. 문의: [전화번호]"
