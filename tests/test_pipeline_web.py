"""문서 자동화 파이프라인 화면: 가져오기 → 과제 생성, 과제 재분석·적용, 권한, 관리 화면."""
from __future__ import annotations

import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from .conftest import csrf, login

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "samples"))
import generate_documents as samples  # noqa: E402


def _run_id(r) -> int:
    return int(re.search(r"/pipeline/(\d+)", str(r.url)).group(1))


def test_import_flow_creates_protocol(app, settings):
    settings.pipeline_sync = True
    with TestClient(app) as c:
        login(c, "researcher")
        assert "연구계획서 가져오기" in c.get("/protocols/import").text
        t = csrf(c)
        r = c.post("/protocols/import", data={"csrf_token": t},
                   files={"file": ("hanbit.docx", samples.build_docx(), "application/octet-stream")})
        assert r.status_code == 200, r.text
        rid = _run_id(r)
        assert "선택 항목으로 새 과제 만들기" in r.text and "1200" in r.text
        api = c.get(f"/api/pipeline/{rid}").json()
        assert api["status"] == "succeeded" and not api["active"]
        assert c.get(f"/pipeline/{rid}/markdown").text.startswith("# hanbit.docx")
        assert c.get(f"/pipeline/{rid}/pdf").content.startswith(b"%PDF")

        r = c.post(f"/pipeline/{rid}/apply-import",
                   data={"csrf_token": t, "fields": ["title", "expected_subjects", "consent_plan"]},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].endswith("/edit")
        pid = int(re.search(r"/protocols/(\d+)/edit", r.headers["location"]).group(1))
        detail = c.get(f"/protocols/{pid}").text
        assert "만성 신장병" in detail

        # 과제 분석 이력·재분석·적용
        page = c.get(f"/protocols/{pid}/pipeline").text
        assert "분석 이력" in page
        r = c.post(f"/protocols/{pid}/pipeline", data={"csrf_token": t})
        rid2 = _run_id(r)
        assert c.get(f"/api/pipeline/{rid2}").json()["status"] in ("succeeded", "partial")
        r = c.post(f"/protocols/{pid}/pipeline/{rid2}/apply",
                   data={"csrf_token": t, "fields": ["inclusion_criteria"], "mode": "fill"})
        assert "반영" in r.text

        # 다른 연구자는 볼 수 없음
        login(c, "researcher2")
        assert c.get(f"/pipeline/{rid}").status_code == 403
        assert c.get(f"/api/pipeline/{rid}").status_code == 403
        assert c.get(f"/protocols/{pid}/pipeline").status_code == 403
        assert c.get("/admin/ai").status_code == 403

        login(c, "coordinator")
        assert c.get(f"/pipeline/{rid}").status_code == 200
        admin = c.get("/admin/ai").text
        assert "AI·문서 자동화 관리" in admin and "전송 정책" in admin


def test_import_rejects_bad_files_and_roles(app, settings):
    settings.pipeline_sync = True
    with TestClient(app) as c:
        login(c, "researcher")
        t = csrf(c)
        r = c.post("/protocols/import", data={"csrf_token": t}, files={"file": ("x.exe", b"MZ", "application/octet-stream")})
        assert "지원하지 않는" in r.text
        r = c.post("/protocols/import", files={"file": ("a.txt", b"x", "text/plain")}, follow_redirects=False)
        assert r.status_code in (400, 403)  # CSRF
        login(c, "reviewer1")
        assert c.get("/protocols/import").status_code == 403
