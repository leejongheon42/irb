from __future__ import annotations

import os
import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from irb_automation.database import SessionLocal
from irb_automation.models import DeidJob, Protocol, ProtocolStatus

from .conftest import csrf, login, make_pdf
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


def test_researcher_end_to_end_over_http(app):
    with TestClient(app) as c:
        login(c, "researcher")
        t = csrf(c)
        form = {
            "csrf_token": t, "_bools": "1", "_lists": "1", "title": "HTTP 테스트 과제", "study_type": "cdw_analysis",
            "objectives": "고혈압 환자에서 약물 순응도와 심혈관 사건의 연관성을 평가한다." * 2,
            "methods": "CDW에서 처방 및 진단 자료를 추출하여 Cox 회귀분석을 시행한다." * 2,
            "inclusion_criteria": "만 19세 이상 고혈압(I10) 진단 환자로 1년 이상 추적된 자",
            "expected_subjects": "3000", "study_start": "2026-12-01", "study_end": "2027-11-30",
            "data_period_start": "2020-01-01", "data_period_end": "2023-12-31", "data_items": "연령\n성별\n처방",
            "deid_level": "pseudonymized", "deid_performed_by": "data_steward", "storage_location": "internal_secure",
            "existing_data_only": "on", "retention_years": "3",
            "consent_plan": "waiver", "waiver_impracticable_reasons": "large_population",
            "waiver_no_presumed_refusal": "on",
        }
        r = c.post("/protocols/new", data=form, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("/package")
        pid = int(r.headers["location"].split("/")[2])
        page = c.get(f"/protocols/{pid}/package")
        assert "필요 서류" in page.text and "자동 완성 실행" in page.text

        # 한 번에 자동 완성: 빈 항목 초안 + 서류 작성 + 점검
        r = c.post(f"/protocols/{pid}/package/autocomplete", data={"csrf_token": t, "draft": "on"}, follow_redirects=True)
        assert "자동 완성 완료" in r.text and "초안 작성 항목" in r.text
        with SessionLocal() as db:
            p = db.get(Protocol, pid)
            assert p.access_controls and p.waiver_impracticable_detail  # 규칙 초안
            types = {d.doc_type for d in p.documents}
            assert {"protocol", "deid_plan"} <= types and types & {"waiver_request", "exemption_request"}
            from irb_automation.qc.models import QCRun

            qr = db.scalars(select(QCRun).where(QCRun.protocol_id == pid)).one()
            rid = qr.id
        page = c.get(f"/qc/{rid}")
        assert page.status_code == 200 and "서류 점검 결과" in page.text

        # 개인 서류 업로드 후 ZIP
        for doc in ("cv", "training_cert", "coi"):
            r = c.post(f"/protocols/{pid}/documents", data={"csrf_token": t, "doc_type": doc},
                       files={"file": (f"{doc}.pdf", make_pdf(), "application/pdf")}, follow_redirects=False)
            assert r.status_code == 303
        bad = c.post(f"/protocols/{pid}/documents", data={"csrf_token": t, "doc_type": "other"},
                     files={"file": ("x.exe", b"MZ", "application/octet-stream")}, follow_redirects=True)
        assert "허용되지 않는 파일" in bad.text
        z = c.get(f"/protocols/{pid}/package.zip")
        assert z.status_code == 200 and z.content[:2] == b"PK"

        # 다른 연구자는 열람 불가
        login(c, "researcher2")
        assert c.get(f"/protocols/{pid}").status_code == 403
        assert c.get(f"/protocols/{pid}/package").status_code == 404
        assert c.get(f"/qc/{rid}").status_code == 404

        # e-IRB 제출 기록 → 보완 요청 → 답변 → 재제출 → 결과
        login(c, "researcher")
        t = csrf(c)
        r = c.post(f"/protocols/{pid}/submit", data={"csrf_token": t, "external_ref": "E-77", "force": "on"},
                   follow_redirects=True)
        assert "제출을 기록했습니다" in r.text and "IRB 결과 입력" in r.text
        r = c.post(f"/protocols/{pid}/revision-request",
                   data={"csrf_token": t, "text": "1. 보관 기간을 명시하십시오.\n2. 동의 면제 사유를 보완하십시오."},
                   follow_redirects=True)
        assert "보완 요청 2개 항목" in r.text and "IRB 보완 요청 대응" in r.text
        page = c.get(f"/protocols/{pid}/revision?draft=1")
        assert "표준 문안" in page.text and "고칠 서류" in page.text
        with SessionLocal() as db:
            from irb_automation.workflow import service as wf

            items = wf.open_revision_items(db, db.get(Protocol, pid))
        data = {"csrf_token": t, **{f"resp_{i.id}": "반영하였습니다." for i in items}}
        assert c.post(f"/protocols/{pid}/revision-responses", data=data, follow_redirects=True).status_code == 200
        r = c.post(f"/protocols/{pid}/generate/revision_response", data={"csrf_token": t}, follow_redirects=True)
        assert "자동 작성하여 첨부" in r.text
        r = c.post(f"/protocols/{pid}/submit", data={"csrf_token": t, "force": "on"}, follow_redirects=True)
        assert "보완 재제출 제출을 기록" in r.text
        r = c.post(f"/protocols/{pid}/result", data={"csrf_token": t, "result": "exempt", "irb_no": "IRB-26-0001"},
                   follow_redirects=True)
        assert "심의면제 확인" in r.text
        with SessionLocal() as db:
            p = db.get(Protocol, pid)
            assert p.status == ProtocolStatus.EXEMPT and p.protocol_no == "IRB-26-0001"
        assert c.get(f"/protocols/{pid}/print/prereview").status_code == 200
        login(c, "admin")
        assert "해시 체인 검증 정상" in c.get("/admin/audit").text


def test_quick_qc_sample_and_autofix(app):
    with TestClient(app) as c:
        login(c, "researcher")
        t = csrf(c)
        r = c.post("/qc", data={"csrf_token": t, "sample": "1"}, follow_redirects=False)
        assert r.status_code == 303
        rid = int(r.headers["location"].rsplit("/", 1)[1])
        page = c.get(f"/qc/{rid}")
        assert "FAIL" in page.text and "투여 용량" in page.text and "파일명과 본문의 버전" in page.text
        ids = re.findall(r'name="issue" value="(\d+)"', page.text)
        assert ids
        r = c.post(f"/qc/{rid}/autofix", data={"csrf_token": t, "issue": ids, "unify_font": "on", "font": "맑은 고딕"},
                   follow_redirects=True)
        assert "자동 교정" in r.text and "변경 추적본" in r.text
        f = c.get(f"/qc/{rid}/file/0/clean")
        assert f.status_code == 200 and f.content[:2] == b"PK"
        assert c.get(f"/qc/{rid}/file/0/tracked").status_code == 200
        login(c, "researcher2")
        assert c.get(f"/qc/{rid}").status_code == 404


def test_deid_tool_for_researcher(app, settings):
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADERS)
    w.writerows(make_rows(120))
    raw = buf.getvalue().encode("utf-8-sig")

    with TestClient(app) as c:
        login(c, "researcher2")
        assert c.get("/deid").status_code == 200
        login(c, "researcher")
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
            analysis = db.get(DeidJob, jid).analysis
        data = {"csrf_token": t, "subject_column": "환자번호", "k_threshold": "5", "reference_date": "2026-01-01"}
        for i, col in enumerate(analysis["columns"]):
            data[f"col_{i}_action"] = col["suggested_action"]
        data["col_3_width"] = "20"
        data["col_6_level"] = "sido"
        data["qi"] = ["생년월일", "성별", "주소"]
        r = c.post(f"/deid/{jid}/process", data=data, follow_redirects=True)
        assert "가명처리 완료" in r.text
        assert c.get(f"/deid/{jid}/report").status_code == 200
        out = c.get(f"/deid/{jid}/download")
        text = out.content.decode("utf-8-sig")
        assert "주민등록번호" not in text.splitlines()[0] and "010-" not in text and "홍길동" not in text

        # 공동연구자는 같은 과제의 가명처리 작업을 볼 수 있음
        login(c, "researcher2")
        assert c.get(f"/deid/{jid}").status_code == 200

        login(c, "researcher")
        t = csrf(c)
        with SessionLocal() as db:
            input_path = db.get(DeidJob, jid).input_path
        c.post(f"/deid/{jid}/purge-input", data={"csrf_token": t})
        assert not Path(input_path).exists()


def test_api_evaluate(app):
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
        assert c.get("/api/integration/approval/IRB-2026-0001").status_code == 404  # 병원 측 연동 API 제거됨


def test_admin_user_management(app):
    with TestClient(app) as c:
        login(c, "admin")
        t = csrf(c)
        r = c.post("/admin/users", data={"csrf_token": t, "username": "newbie", "full_name": "신규", "role": "researcher",
                                         "password": "short"}, follow_redirects=True)
        assert "비밀번호 정책 미충족" in r.text
        r = c.post("/admin/users", data={"csrf_token": t, "username": "newbie", "full_name": "신규", "role": "researcher",
                                         "password": "Strong!pass1"}, follow_redirects=True)
        assert "newbie 생성 완료" in r.text
        assert c.get("/admin/rules").status_code == 200
        login(c, "researcher")
        assert c.get("/admin/users").status_code == 403


def test_all_pages_render_for_each_role(app):
    with TestClient(app) as c:
        for username in ("admin", "researcher", "researcher2"):
            login(c, username)
            for url in ("/", "/protocols", "/notifications", "/qc", "/knowledge", "/doc-templates", "/deid"):
                assert c.get(url).status_code == 200, (username, url)
        login(c, "researcher")
        for url in ("/protocols/new", "/protocols/1", "/protocols/1/edit", "/protocols/1/precheck",
                    "/protocols/1/package", "/protocols/1/revision", "/protocols/2", "/protocols/1/report/continuing"):
            assert c.get(url).status_code == 200, url
        for gone in ("/meetings", "/drb", "/outputs", "/governance/agreements", "/reports/kpi"):
            assert c.get(gone).status_code == 404, gone
    assert os.environ.get("IRB_INTEGRATION_TOKEN") is None
