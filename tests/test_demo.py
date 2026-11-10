"""화면별 가이드, 샘플 데이터 자료실, 전체 파이프라인 테스트."""
from __future__ import annotations

from fastapi.testclient import TestClient

from irb_automation.demo.samples import SAMPLES
from irb_automation.help.guides import BY_KEY, GUIDES, PATTERNS, guide_for

from .conftest import csrf, login


def test_guides_cover_main_pages_and_are_complete():
    for path, key in [("/", "dashboard"), ("/protocols/3/package", "package"), ("/qc/5", "qc_run"), ("/qc", "qc_home"),
                      ("/protocols/2/revision", "revision"), ("/protocols/2", "protocol_detail"),
                      ("/protocols/new", "protocol_new"), ("/protocols/import", "import"), ("/demo", "demo"),
                      ("/samples", "samples"), ("/admin/templates", "admin_templates")]:
        assert guide_for(path).key == key, path
    assert guide_for("/nope") is None
    assert {k for _, k in PATTERNS} <= set(BY_KEY)
    for g in GUIDES:
        assert g.title and g.what and g.first, g.key


def test_samples_all_build():
    for s in SAMPLES:
        data = s.build()
        assert len(data) > 100, s.key


def test_guide_panel_and_pages_over_http(app):
    with TestClient(app) as c:
        login(c, "researcher")
        home = c.get("/")
        assert 'id="page-guide"' in home.text and 'data-guide-key="dashboard"' in home.text
        assert "처음 사용하시나요?" in home.text
        assert 'data-guide-key="package"' in c.get("/protocols/1/package").text
        assert c.get("/help").status_code == 200 and "추천 순서" in c.get("/help").text
        page = c.get("/samples")
        assert page.status_code == 200 and "점검용 동의서" in page.text
        assert "기관 서식 예시" not in page.text  # 관리자용 샘플은 숨김
        f = c.get("/samples/qc_icf")
        assert f.status_code == 200 and f.content[:2] == b"PK" and "ICF_v3.0.docx" in f.headers["content-disposition"]
        assert c.get("/samples/nope").status_code == 404
        # 관리자 전용 가이드는 연구자에게 표시하지 않음
        assert c.get("/admin/templates").status_code == 403
        login(c, "admin")
        assert 'data-guide-key="admin_templates"' in c.get("/admin/templates").text
        assert "기관 서식 예시" in c.get("/samples").text


def test_full_pipeline_over_http(app):
    with TestClient(app) as c:
        login(c, "researcher")
        assert "전체 파이프라인 실행" in c.get("/demo").text
        t = csrf(c)
        r = c.post("/demo/run", data={"csrf_token": t}, follow_redirects=True)
        assert r.status_code == 200 and "전체 파이프라인 테스트 결과" in r.text
        assert r.text.count('class="demo-step"') == 15
        assert "실패 0" in r.text
        assert "IRB 결과 &#39;승인&#39;" in r.text or "IRB 결과 '승인'" in r.text
        did = int(str(r.url).rstrip("/").rsplit("/", 1)[1])
        login(c, "researcher2")
        assert c.get(f"/demo/{did}").status_code == 404


def test_full_pipeline_service(db, users):
    from irb_automation.demo.full import run_full_pipeline
    from irb_automation.models import ProtocolStatus

    d = run_full_pipeline(db, users["researcher"])
    assert d.summary["failed"] == 0, d.steps
    assert d.protocol.status == ProtocolStatus.APPROVED and d.protocol.title.startswith("[파이프라인 테스트]")
    by_title = {s["title"]: s for s in d.steps}
    inj = by_title["오류 주입 후 서류 교차 점검"]
    if inj["status"] != "skipped":  # LibreOffice가 있으면 DOCX로 오류 주입·교정·재점검까지 검증
        assert d.summary["qc_after"] > d.summary["qc_before"]
        assert "모두 해소" in by_title["교정본 재점검"]["message"]
