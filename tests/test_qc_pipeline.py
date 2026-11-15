import pytest

from irb_automation.qc import checks, review, service
from irb_automation.qc.bundle import inspect_bundle
from irb_automation.qc.profile import build_profile
from irb_automation.qc.rules import rules


def test_cross_check_requires_explicit_reference(db, users):
    docs = [checks.QCDoc(f'd{i}', f'{i}.txt', role, f'목표 연구대상자 수: 총 {count}명')
            for i, (role, count) in enumerate((('protocol', 120), ('application', 100), ('consent_form', 120)), 1)]
    qr = service.run(db, users['researcher'], docs, use_ai=False)
    issue, = [i for i in qr.issues if i['code'] == 'CROSS_SUBJECTS']
    assert issue['rule_id'] == 'E02' and len(issue['locations']) == 3
    assert issue['fixes'] == [] and qr.score == 100 and qr.verdict == 'HOLD'
    selected = review.select_reference(issue, 0)
    assert selected['fixes'] == [{'doc': 'd2', 'old': '목표 연구대상자 수: 총 100명',
                                  'new': '목표 연구대상자 수: 총 120명'}]


def test_scope_and_unreadable_bundle(db, users):
    docs = [checks.QCDoc('p', '계획서.txt', 'protocol', '연구대상자 수: 총 120명'),
            checks.QCDoc('a', '신청서.txt', 'application', '기관별 대상자 수: 40명'),
            checks.QCDoc('c', '동의서.pdf', 'consent_form', '')]
    qr = service.run(db, users['researcher'], docs, use_ai=False, profile={'consent': ['obtain']})
    assert not any(i['code'] == 'CROSS_SUBJECTS' for i in qr.issues)
    assert any(i.get('reason') == 'unreadable' for i in qr.issues)
    assert qr.score == 100 and qr.verdict == 'HOLD'


def test_review_score_excludes_candidates_and_duplicates():
    from irb_automation.qc.scoring import assess

    major = {'severity': 'major', 'review': 'confirmed', 'group': 'E02|total'}
    minor = {'severity': 'minor', 'review': 'candidate'}
    assert assess([major, major])['score'] == 93
    assert assess([major])['status'] == 'REVISE'
    assert assess([major, minor])['status'] == 'HOLD'
    assert assess([{**minor, 'review': 'accepted'}])['status'] == 'PASS'


def test_revision_history_is_not_current_version():
    doc = checks.QCDoc('p', '계획서_v3.0.txt', 'protocol',
                       '현재 버전: v3.0\n개정 이력\n2025년 | v1.0\n2026년 | v2.0')
    assert checks.check_version(doc) == []


@pytest.mark.parametrize(("answers", "missing"), [
    ({"application": "new", "method": ["survey"], "consent": ["obtain"]},
     {"questionnaire", "training_cert", "consent_form"}),
    ({"application": "new", "method": ["survey"], "data_source": ["collect"], "consent": ["obtain"]},
     {"questionnaire", "training_cert", "consent_form"}),
    ({"application": "new", "method": ["intervention"], "data_source": ["collect"], "consent": ["obtain"]},
     {"training_cert", "consent_form"}),
    ({"application": "new", "method": ["survey"], "consent": ["waiver_request"]},
     {"questionnaire", "training_cert", "waiver_request"}),
    ({"application": "new", "method": ["records"], "data_source": ["existing_internal"], "consent": ["waiver_request"]},
     {"training_cert", "waiver_request"}),
    ({"application": "new", "study_type": "specimen", "method": ["specimen"], "data_source": ["collect"], "consent": ["obtain"]},
     {"training_cert", "consent_form", "specimen_consent"}),
    ({"application": "exemption", "method": ["records"], "consent": ["not_applicable"]},
     {"exemption_request"}),
], ids=["1-in-person-survey", "2-online-survey", "3-intervention", "5-waiver", "8-records",
        "17-specimen", "30-exemption"])
def test_public_irb_bundle_by_application_route(answers, missing):
    docs = [checks.QCDoc("p", "계획서.txt", "protocol", "합성 연구계획서")]
    profile = build_profile(answers, None, docs)
    units, _ = inspect_bundle(docs, profile)
    assert {u["field"] for u in units if u["reason"] == "file_missing"} == missing


def test_profile_user_answer_overrides_detected_medication(db, users):
    doc = checks.QCDoc("p", "계획서.txt", "protocol", "시험약: SAMPLE-1\n1일 1회 30mg 경구 투여")
    qr = service.run(db, users["researcher"], [doc], use_ai=False, profile={"drug": False})
    assert qr.profile["source"]["drug"] == "user"
    assert {u["rule"] for u in qr.checks if u["result"] == "na"} >= {"M02", "M03"}


@pytest.mark.parametrize(("left", "right", "codes"), [
    ("1회 0.1 g 경구 투여", "1회 100 mg PO 투여", set()),
    ("1일 1회 100mg 경구 투여", "1일 2회 100mg 경구 투여", {"CROSS_DOSE_FREQ"}),
    ("1회 100mg", "1일 200mg", set()),
    ("참여 기간은 총 12주", "연구 기간: 2026.01.01 ~ 2027.12.31", set()),
    ("총 4회 방문", "선택 방문 횟수: 6회", set()),
    ("권리 문의: 02-123-4567", "권리 문의: 02) 123-4567", set()),
    ("교통비 2만원", "사례비 10000원", set()),
    ("처리 수준: 가명", "처리 수준: 익명", {"CROSS_DEID_LEVEL"}),
])
def test_only_compatible_facts_compared(left, right, codes):
    docs = [checks.QCDoc("p", "계획서.txt", "protocol", left),
            checks.QCDoc("c", "동의서.txt", "consent_form", right)]
    issues, _ = checks.run_rules(docs, None)
    assert {i["code"] for i in issues if i["category"] == "cross" and i["result"] == "mismatch"} == codes


def test_registered_manual_flags():
    registry = rules()["rules"]
    assert sum(len(registry[f"M{i:02}"]["flags"]) for i in range(1, 11)) == 52


@pytest.mark.parametrize(("text", "expected"), [
    ("1회 100mg, 1일 2회, 1일 총 100mg", {"DOSE_SUM"}),
    ("1회 100mg, 1일 2회, 1일 총 200mg", set()),
    ("필요 시 1회 100mg, 1일 2회, 1일 총 100mg", set()),
    ("시험군 60명, 대조군 60명, 총 100명", {"SUBJECTS_SUM"}),
    ("시험군 60명, 대조군 60명, 총 120명", set()),
])
def test_explicit_totals(text, expected):
    _, phase3 = checks.run_rules([checks.QCDoc("p", "계획서.txt", "protocol", text)], None)
    assert {i["code"] for i in phase3 if i["code"] in ("DOSE_SUM", "SUBJECTS_SUM")} == expected


def test_existing_sqlite_qc_run_gets_profile_and_checks(tmp_path):
    from sqlalchemy import create_engine, text

    from irb_automation.database import migrate_columns

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE qc_runs (id INTEGER PRIMARY KEY, verdict VARCHAR(8), created_at DATETIME)"))
            conn.execute(text("INSERT INTO qc_runs (id, verdict) VALUES (3, 'PASS')"))
        added = migrate_columns(engine)
        assert {"qc_runs.profile", "qc_runs.checks"} <= set(added)
        with engine.connect() as conn:
            assert conn.execute(text("SELECT verdict, profile, checks FROM qc_runs WHERE id=3")).one() == ("PASS", "{}", "[]")
    finally:
        engine.dispose()
