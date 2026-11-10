"""데모 데이터. 모든 인물·과제는 가상이며 실제 환자 정보를 포함하지 않는다."""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Document, Protocol, Role, User
from .security import hash_password

DEMO_PASSWORD = None  # None이면 데모 계정 비밀번호 = 아이디 (researcher/researcher, admin/admin)

DEMO_USERS = [
    ("admin", "연구지원관리자", "연구지원팀", Role.ADMIN),
    ("researcher", "정연구", "내분비내과", Role.RESEARCHER),
    ("researcher2", "한공동", "소아청소년과", Role.RESEARCHER),
]


def _doc(p: Protocol, doc_type: str, uid: int) -> Document:
    return Document(
        protocol_id=p.id, doc_type=doc_type, filename=f"{doc_type}_sample.pdf", stored_path="",
        sha256="0" * 64, size=0, version=1, uploaded_by=uid,
    )


def seed_demo(db: Session, password: str | None = DEMO_PASSWORD) -> dict[str, User]:
    users: dict[str, User] = {}
    for username, name, dept, role in DEMO_USERS:
        u = db.scalars(select(User).where(User.username == username)).first()
        if u is None:
            u = User(
                username=username, password_hash=hash_password(password or username), full_name=name, department=dept, role=role,
                training_expires_on=date.today() + timedelta(days=400),
            )
            db.add(u)
        users[username] = u
    db.flush()

    if db.scalars(select(Protocol)).first():
        return users

    r = users["researcher"]
    # 1) 가명정보 후향적 연구 - 심의면제 후보
    p1 = Protocol(
        title="제2형 당뇨병 환자의 SGLT2 억제제 사용과 급성 신손상 발생의 연관성: 후향적 코호트 연구",
        pi_id=r.id, co_investigators=[users["researcher2"].id], department="내분비내과",
        study_type="cdw_analysis",
        objectives="SGLT2 억제제를 처방받은 제2형 당뇨병 환자에서 DPP-4 억제제 대비 급성 신손상(AKI) 발생 위험을 비교한다.",
        background="SGLT2 억제제의 신장 보호 효과가 보고되었으나 실제 임상 환경에서 초기 AKI 위험에 대한 근거는 제한적이다.",
        methods="CDW에서 2019-2024년 처방 이력을 추출하여 성향점수 매칭 후 Cox 비례위험모형으로 AKI 발생 위험비를 추정한다. "
                "데이터관리자가 가명처리한 데이터만 원내 분석서버에서 분석한다.",
        target_population="2019-01-01 ~ 2024-12-31 본원에서 SGLT2 억제제 또는 DPP-4 억제제를 신규 처방받은 성인 제2형 당뇨병 환자",
        inclusion_criteria="만 19세 이상, KCD E11 진단, 연구 약물 신규 처방, 처방 전 1년 이내 혈청 크레아티닌 측정 기록 보유",
        exclusion_criteria="투석 환자, 신장이식 환자, 처방 전 eGFR 30 미만",
        expected_subjects=8500,
        sample_size_justification="연구 기간 내 적격 환자 전수. 예비 조사에서 약 8,500명 확인, AKI 발생률 3% 가정 시 검정력 90% 확보.",
        study_start=date.today() + timedelta(days=14), study_end=date.today() + timedelta(days=700),
        data_sources=["cdw", "lis"], existing_data_only=True,
        data_period_start=date(2019, 1, 1), data_period_end=date(2024, 12, 31),
        data_items="연구용 일련번호\n연령(5세 구간)\n성별\n처방 약물·기간\nHbA1c\n혈청 크레아티닌·eGFR\n동반질환 KCD 코드\n입원 여부",
        deid_level="pseudonymized", deid_performed_by="data_steward", storage_location="internal_secure",
        access_controls="원내 폐쇄망 분석서버에서만 분석, 연구자 계정별 접근권한 부여, 반출 시 데이터센터 승인, 접근 로그 1년 보관",
        retention_years=3, destruction_method="연구 종료 3년 후 분석서버에서 복구 불가능하게 영구 삭제하고 파기 확인서를 IRB에 제출",
        consent_plan="waiver", waiver_impracticable_reasons=["large_population", "deceased_or_lost"],
        waiver_impracticable_detail="8,500명 규모의 과거 진료기록으로 상당수가 추적 불가 또는 사망하여 개별 동의 획득이 현실적으로 불가능함",
        waiver_no_presumed_refusal=True,
        waiver_minimal_risk_detail="데이터관리자가 가명처리한 정보만 이용하며 연구대상자 접촉이 없어 위험이 극히 낮음",
    )
    db.add(p1)
    db.flush()
    for d in ("exemption_request", "protocol", "cv", "training_cert", "coi", "crf", "deid_plan", "security_pledge"):
        db.add(_doc(p1, d, r.id))

    # 2) 정신건강 자유기술 + AI + 외부 반출 - 정규심의 후보
    p2 = Protocol(
        title="정신건강의학과 경과기록 자연어처리를 이용한 자살 위험 예측 AI 모델 개발",
        pi_id=r.id, department="정신건강의학과", study_type="imaging_ai", funding_source="○○ 헬스케어(주)",
        objectives="정신건강의학과 외래·입원 경과기록을 학습하여 6개월 내 자살 시도 위험을 예측하는 언어모델을 개발한다.",
        methods="경과기록 원문과 진단·처방 정보를 추출하여 공동연구기관(○○ 헬스케어)의 GPU 클라우드에서 모델을 학습한다.",
        target_population="2015-2024년 정신건강의학과 진료 환자", inclusion_criteria="정신건강의학과 진료 1회 이상",
        expected_subjects=120000, data_sources=["emr"],
        data_period_start=date(2015, 1, 1), data_period_end=date(2024, 12, 31),
        data_items="환자번호\n성별\n생년월일\n경과기록 원문\n진단코드\n처방",
        identifiers_collected=["mrn", "birth_date"], sensitive_categories=["mental_health"],
        vulnerable_populations=["children"], unstructured_data=["free_text"],
        deid_level="pseudonymized", deid_performed_by="researcher", storage_location="external_cloud",
        external_transfer=True, external_recipient="○○ 헬스케어(주)", ai_ml_development=True, commercial_use=True,
        retention_years=10, consent_plan="waiver", waiver_impracticable_reasons=["large_population"],
    )
    db.add(p2)
    db.flush()
    return users


def seed_rich_demo(db: Session) -> None:
    """화면 시연용 진행 데이터: IRB 면제 확인을 받은 과제, 보완 요청을 받은 과제."""
    from .workflow import service as wf

    if db.scalars(select(Protocol).where(Protocol.title.contains("PEWS"))).first():
        return
    u = {x.username: x for x in db.scalars(select(User))}
    p1 = db.scalars(select(Protocol).where(Protocol.title.contains("SGLT2"))).first()
    if p1 and p1.status.value == "draft":
        wf.record_submission(db, u["researcher"], p1, external_ref="E-2026-0412", force=True, ip="seed")
        wf.record_result(db, u["researcher"], p1, "exempt", irb_no="IRB-2026-0412", note="심의면제 확인", ip="seed")

    p3 = Protocol(
        title="소아 중환자실 패혈증 환자의 조기 경고 점수(PEWS) 검증: 식별정보 연계 후향적 연구",
        pi_id=u["researcher2"].id, co_investigators=[u["researcher"].id], department="소아청소년과",
        study_type="retrospective_chart_review",
        objectives="소아 중환자실 입실 환자에서 조기 경고 점수(PEWS)의 패혈증 예측 성능을 검증한다.",
        background="소아 패혈증은 조기 인지가 예후를 좌우하나 PEWS의 국내 PICU 검증 자료는 부족하다.",
        methods="EMR에서 활력징후·검사결과를 추출하고, 사망·전원 정보 연계를 위해 등록번호를 사용한다. ROC 분석을 시행한다.",
        target_population="2018-2024년 소아 중환자실 입실 환자", inclusion_criteria="만 18세 이하, PICU 입실 24시간 이상인 환자로 한정함",
        exclusion_criteria="입실 24시간 이내 전원 환자",
        expected_subjects=1800, sample_size_justification="연구 기간 내 적격 환자 전수(약 1,800명)",
        study_start=date.today() + timedelta(days=30), study_end=date.today() + timedelta(days=500),
        data_sources=["emr", "lis"], data_period_start=date(2018, 1, 1), data_period_end=date(2024, 12, 31),
        data_items="등록번호\n연령(월)\n성별\n활력징후\n검사결과\n진단코드\n사망 여부",
        identifiers_collected=["mrn"], identifier_justification="퇴원 후 사망·전원 정보 연계를 위해 등록번호가 필요하며 연계 후 즉시 일련번호로 대체함",
        vulnerable_populations=["children"], deid_level="identifiable", storage_location="internal_secure",
        access_controls="원내 분석서버 전용 계정, 연구진 2인 한정 접근, 반출 시 데이터센터 승인, 접근 로그 보관",
        retention_years=3, destruction_method="연구 종료 3년 후 분석서버에서 영구 삭제 후 확인서 제출",
        consent_plan="obtain",
    )
    db.add(p3)
    db.flush()
    for d in ("application", "protocol", "cv", "training_cert", "coi", "crf", "deid_plan", "security_pledge", "consent_form"):
        db.add(_doc(p3, d, p3.pi_id))
    db.flush()
    db.refresh(p3)
    wf.record_submission(db, u["researcher2"], p3, external_ref="E-2026-0533", force=True, ip="seed")
    wf.record_revision_request(db, u["researcher2"], p3, (
        "1. 소아 대상 식별정보(등록번호) 이용에 대한 추가 보호대책(연계 후 즉시 가명화 절차)을 명시하십시오.\n"
        "2. 연구대상자 동의서에 법정대리인 동의 및 만 7세 이상 아동의 assent 절차를 추가하십시오.\n"
        "3. 연구계획서와 동의서의 연구 기간 표기가 다릅니다. 일치시키십시오."
    ), ip="seed")
