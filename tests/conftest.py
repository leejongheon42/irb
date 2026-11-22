from __future__ import annotations

import os
import re
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from irb_automation.config import Settings, normalize_database_url, override_settings
from irb_automation.database import SessionLocal, init_db, reset_engine
from irb_automation.engine.facts import ProtocolFacts

PASSWORD = "Demo!2026pw"


def make_pdf(pages: int = 1, text: str = "") -> bytes:
    """검증을 통과하는 실제 PDF(업로드 검증이 손상 PDF를 거부하므로 테스트용)."""
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for i in range(pages):
        for j, line in enumerate((text or f"page {i + 1}").splitlines()):
            c.drawString(72, 760 - 16 * j, line)
        c.showPage()
    c.save()
    return buf.getvalue()


# PostgreSQL에서도 전체 테스트를 돌리려면 IRB_TEST_DATABASE_URL=postgresql://... 을 지정한다(테스트마다 스키마 초기화).
TEST_DATABASE_URL = os.environ.get("IRB_TEST_DATABASE_URL", "")


def _reset_postgres_schema(url: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(normalize_database_url(url))
    with engine.begin() as conn:
        # 이전 테스트의 백그라운드 작업이 남긴 연결이 잠금을 잡고 있으면 스키마 삭제가 교착된다.
        conn.execute(text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                          "WHERE datname = current_database() AND pid <> pg_backend_pid()"))
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    for name in ("IRB_DATABASE_URL", "DATABASE_URL", "POSTGRES_URL", "IRB_FILE_STORE"):
        monkeypatch.delenv(name, raising=False)
    if TEST_DATABASE_URL:
        _reset_postgres_schema(TEST_DATABASE_URL)
    s = Settings(data_dir=tmp_path / "data", password_iterations=1000, scheduler_enabled=False, mvp_mode=False,
                 builtin_accounts=False, database_url=TEST_DATABASE_URL)
    override_settings(s)
    reset_engine()
    init_db()
    yield s
    reset_engine()


@pytest.fixture()
def db(settings):
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def users(db):
    from irb_automation.seed import seed_demo

    u = seed_demo(db, PASSWORD)
    db.commit()
    return u


@pytest.fixture()
def app(settings, users):
    from irb_automation.main import create_app

    return create_app()


def login(client: TestClient, username: str) -> None:
    client.cookies.clear()
    page = client.get("/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    r = client.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": token},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/", r.text


def csrf(client: TestClient) -> str:
    page = client.get("/notifications")
    return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)


def good_facts(**overrides) -> ProtocolFacts:
    """심의면제 요건을 충족하는 가명정보 후향적 연구."""
    base = dict(
        title="후향적 연구",
        objectives="연구 목적을 구체적으로 기술함 " * 3,
        methods="자료 추출 및 통계 분석 방법을 기술함 " * 3,
        inclusion_criteria="선정기준 " * 8,
        data_items="연령\n성별\n검사결과",
        expected_subjects=1000,
        sample_size_justification="연구 기간 내 적격 환자 전수 조사로 산출 근거를 기재함",
        data_period_start=date(2020, 1, 1),
        data_period_end=date(2023, 12, 31),
        deid_level="pseudonymized",
        deid_performed_by="data_steward",
        access_controls="원내 폐쇄망 분석서버에서만 분석하며 접근권한을 최소화하고 로그를 보관함",
        destruction_method="연구 종료 후 복구 불가능한 방법으로 영구 삭제하고 확인서를 제출함",
        consent_plan="waiver",
        waiver_impracticable_reasons=["large_population"],
        waiver_impracticable_detail="대상자가 1,000명 이상이며 과거 자료로 연락처가 부정확하여 동의 획득이 현실적으로 불가능함",
        waiver_no_presumed_refusal=True,
        pi_training_expires_on=date.today() + timedelta(days=100),
        uploaded_doc_types={
            "exemption_request", "application", "protocol", "cv", "training_cert", "coi", "crf",
            "deid_plan", "security_pledge", "waiver_request",
        },
    )
    base.update(overrides)
    return ProtocolFacts(**base)
