from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from irb_automation.database import SessionLocal
from irb_automation.models import DeidJob, Protocol, ProtocolStatus

from .conftest import csrf, login
from .test_deid import HEADERS, make_rows


def test_login_required_and_csrf(app):
    with TestClient(app) as c:
        assert c.get("/", follow_redirects=False).headers["location"] == "/login"
        assert c.post("/api/prereview/evaluate", json={}).status_code == 401
        login(c, "researcher")
        r = c.post("/protocols/new", data={"title": "csrf 없음"}, follow_redirects=False)
        assert r.status_code == 403
        r = c.get("/")
        assert "Content-Security-Policy" in r.headers and r.headers["X-Frame-Options"] == "DENY"


def test_bad_password_rejected(app):
    with TestClient(app) as c:
        page = c.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        r = c.post("/login", data={"username": "researcher", "password": "wrong", "csrf_token": token},
                   follow_redirects=False)
        assert r.headers["location"] == "/login"


def test_end_to_end_review_over_http(app):
    with TestClient(app) as c:
        # 연구자: 과제 작성 → 서류 업로드 → 제출
        login(c, "researcher")
        t = csrf(c)
        form = {
            "csrf_token": t, "_bools": "1", "_lists": "1", "title": "HTTP 테스트 과제", "study_type": "cdw_analysis",
            "objectives": "고혈압 환자에서 약물 순응도와 심혈관 사건의 연관성을 평가한다." * 2,
            "methods": "CDW에서 처방 및 진단 자료를 추출하여 Cox 회귀분석을 시행한다." * 2,
            "inclusion_criteria": "만 19세 이상 고혈압(I10) 진단 환자로 1년 이상 추적된 자",
            "expected_subjects": "3000", "sample_size_justification": "연구 기간 내 적격 환자 전수 조사",
            "data_period_start": "2020-01-01", "data_period_end": "2023-12-31", "data_items": "연령\n성별\n처방",
            "deid_level": "pseudonymized", "deid_performed_by": "data_steward", "storage_location": "internal_secure",
            "existing_data_only": "on", "retention_years": "3",
            "access_controls": "원내 분석서버에서만 분석하고 접근권한을 최소화하며 접근 기록을 보관함",
            "destruction_method": "연구 종료 후 복구 불가능하게 영구 삭제하고 파기 확인서를 제출함",
            "consent_plan": "waiver", "waiver_impracticable_reasons": "large_population",
            "waiver_impracticable_detail": "3,000명 규모의 과거 자료로 개별 연락 및 동의 획득이 현실적으로 불가능함",
            "waiver_no_presumed_refusal": "on",
        }
        r = c.post("/protocols/new", data=form, follow_redirects=False)
        assert r.status_code == 303
        pid = int(r.headers["location"].rsplit("/", 1)[1])
        page = c.get(f"/protocols/{pid}")
        assert "필수 서류 누락" in page.text and "심의면제" in page.text

        for doc in ("exemption_request", "protocol", "cv", "training_cert", "coi", "crf", "deid_plan", "security_pledge"):
            r = c.post(f"/protocols/{pid}/documents", data={"csrf_token": t, "doc_type": doc},
                       files={"file": (f"{doc}.pdf", b"%PDF-1.4 test", "application/pdf")}, follow_redirects=False)
            assert r.status_code == 303
        bad = c.post(f"/protocols/{pid}/documents", data={"csrf_token": t, "doc_type": "other"},
                     files={"file": ("x.exe", b"MZ", "application/octet-stream")}, follow_redirects=True)
        assert "허용되지 않는 파일" in bad.text

        r = c.post(f"/protocols/{pid}/submit", data={"csrf_token": t}, follow_redirects=True)
        assert "신규심의 신청이 접수되었습니다" in r.text
        with SessionLocal() as db:
            p = db.get(Protocol, pid)
            assert p.status == ProtocolStatus.SUBMITTED and p.review_type.value == "exempt"
            doc_id = p.documents[0].id

        # 다른 연구자는 열람 불가
        login(c, "researcher2")
        assert c.get(f"/protocols/{pid}").status_code == 403
        assert c.get(f"/documents/{doc_id}").status_code == 403

        # 간사: 면제 확인
        login(c, "coordinator")
        t = csrf(c)
        dash = c.get("/")
        assert "HTTP 테스트 과제" in dash.text
        r = c.post(f"/protocols/{pid}/decide", data={"csrf_token": t, "decision": "exempt_confirm"}, follow_redirects=True)
        assert "심의면제 확인" in r.text
        with SessionLocal() as db:
            p = db.get(Protocol, pid)
            assert p.status == ProtocolStatus.EXEMPT
            did = p.submissions[-1].decision.id
        letter = c.get(f"/decisions/{did}/letter")
        assert letter.status_code == 200 and "심의면제 확인서" in letter.text
        assert c.get(f"/protocols/{pid}/print/prereview").status_code == 200
        audit_page = c.get("/admin/audit")
        assert "해시 체인 검증 정상" in audit_page.text

        # 연구자: 문서 다운로드 가능
        login(c, "researcher")
        assert c.get(f"/documents/{doc_id}").content == b"%PDF-1.4 test"


def test_deid_flow_and_release_gate(app, settings):
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADERS)
    w.writerows(make_rows(120))
    raw = buf.getvalue().encode("utf-8-sig")

    with TestClient(app) as c:
        login(c, "researcher")
        assert c.get("/deid").status_code == 403

        login(c, "steward")
        t = csrf(c)
        with SessionLocal() as db:
            p = db.scalars(select(Protocol).where(Protocol.title.contains("SGLT2"))).one()
            pid = p.id
        r = c.post("/deid/new", data={"csrf_token": t, "protocol_id": str(pid)},
                   files={"file": ("patients.csv", raw, "text/csv")}, follow_redirects=False)
        assert r.status_code == 303
        jid = int(r.headers["location"].rsplit("/", 1)[1])
        page = c.get(f"/deid/{jid}")
        assert "주민등록번호" in page.text and "직접 식별자" in page.text
        assert "800101" not in page.text

        with SessionLocal() as db:
            job = db.get(DeidJob, jid)
            analysis = job.analysis
        data = {"csrf_token": t, "subject_column": "환자번호", "k_threshold": "5", "reference_date": "2026-01-01"}
        for i, col in enumerate(analysis["columns"]):
            data[f"col_{i}_action"] = col["suggested_action"]
        data["col_3_width"] = "20"
        data["col_6_level"] = "sido"
        data["qi"] = ["생년월일", "성별", "주소"]
        r = c.post(f"/deid/{jid}/process", data=data, follow_redirects=True)
        assert "가명처리 완료" in r.text

        # 승인 전 과제에는 제공 불가
        r = c.post(f"/deid/{jid}/release", data={"csrf_token": t}, follow_redirects=True)
        assert "IRB 승인이 유효하지 않아" in r.text

        with SessionLocal() as db:
            p = db.get(Protocol, pid)
            from irb_automation.models import DecisionType, User
            from irb_automation.workflow import service as wf

            wf.submit(db, db.get(User, p.pi_id), p)
            coord = db.scalars(select(User).where(User.username == "coordinator")).one()
            wf.decide(db, coord, p, DecisionType.EXEMPT_CONFIRM)
            db.commit()

        r = c.post(f"/deid/{jid}/release", data={"csrf_token": t}, follow_redirects=True)
        assert "연구팀에 가명정보를 제공했습니다" in r.text
        assert c.get(f"/deid/{jid}/report").status_code == 200

        # 연구팀 다운로드 - 식별정보 없음
        login(c, "researcher")
        out = c.get(f"/deid/{jid}/download")
        assert out.status_code == 200
        text = out.content.decode("utf-8-sig")
        assert "주민등록번호" not in text.splitlines()[0] and "010-" not in text and "홍길동" not in text

        # 원본 파기
        login(c, "steward")
        t = csrf(c)
        with SessionLocal() as db:
            input_path = db.get(DeidJob, jid).input_path
        c.post(f"/deid/{jid}/purge-input", data={"csrf_token": t})
        assert not Path(input_path).exists()


def test_api_evaluate_and_integration(app, monkeypatch):
    with TestClient(app) as c:
        assert c.get("/api/health").json()["status"] == "ok"
        login(c, "researcher")
        r = c.post("/api/prereview/evaluate", json={"deid_level": "identifiable", "identifiers_collected": ["rrn"],
                                                     "vulnerable_populations": ["children"]},
                   headers={"x-csrf-token": "unused"})
        assert r.status_code == 200
        body = r.json()
        assert body["review_type"] == "full"
        assert any(f["code"] == "RRN_COLLECTION" for f in body["findings"])

        assert c.get("/api/integration/approval/IRB-2026-0001").status_code == 503
        monkeypatch.setenv("IRB_INTEGRATION_TOKEN", "tok123")
        assert c.get("/api/integration/approval/IRB-2026-0001", headers={"X-IRB-Token": "bad"}).status_code == 401
        assert c.get("/api/integration/approval/NOPE", headers={"X-IRB-Token": "tok123"}).status_code == 404


def test_admin_user_management(app):
    with TestClient(app) as c:
        login(c, "admin")
        t = csrf(c)
        r = c.post("/admin/users", data={"csrf_token": t, "username": "newbie", "full_name": "신규", "role": "reviewer",
                                         "password": "short"}, follow_redirects=True)
        assert "비밀번호 정책 미충족" in r.text
        r = c.post("/admin/users", data={"csrf_token": t, "username": "newbie", "full_name": "신규", "role": "reviewer",
                                         "password": "Strong!pass1"}, follow_redirects=True)
        assert "newbie 생성 완료" in r.text
        assert c.get("/admin/rules").status_code == 200
        login(c, "researcher")
        assert c.get("/admin/users").status_code == 403


def test_all_pages_render_for_each_role(app):
    with TestClient(app) as c:
        for username in ("admin", "coordinator", "chair", "reviewer1", "researcher", "steward"):
            login(c, username)
            for url in ("/", "/protocols", "/notifications"):
                assert c.get(url).status_code == 200, (username, url)
        login(c, "researcher")
        for url in ("/protocols/new", "/protocols/1", "/protocols/1/edit", "/protocols/1/precheck",
                    "/protocols/2", "/protocols/1/report/continuing"):
            assert c.get(url).status_code == 200, url
    assert os.environ.get("IRB_INTEGRATION_TOKEN") is None
