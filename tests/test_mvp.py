from __future__ import annotations

import io
import re
from datetime import timedelta
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from reportlab.pdfgen.canvas import Canvas
from sqlalchemy import select

from irb_automation.ai import client as ai_client
from irb_automation.mvp.models import MvpUpload
from irb_automation.qc.category_scores import score_run
from irb_automation.qc.checks import criteria
from irb_automation.qc.models import QCRun
from irb_automation.submission.validate import validate_upload
from irb_automation.timeutil import now

from .conftest import PASSWORD, make_pdf
from .test_submission import docx_bytes


def _token(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^\"]+)"', html)
    assert match
    return match[1]


def _login(client: TestClient, username: str = "researcher") -> str:
    login = client.get("/login")
    result = client.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": _token(login.text)},
                         follow_redirects=False)
    assert result.status_code == 303 and result.headers["location"] == "/mvp"
    assert client.get("/", follow_redirects=False).headers["location"] == "/mvp"
    return _token(client.get("/mvp").text)


@pytest.fixture
def client(app, settings):
    settings.mvp_mode = True
    settings.pipeline_sync = True
    settings.ai_review_enabled = False
    with TestClient(app) as client:
        yield client


def _upload(client: TestClient, token: str, *files: tuple[str, bytes]):
    response = client.post("/mvp/files", data={"csrf_token": token}, files=[("files", file) for file in files],
                           headers={"Accept": "application/json"})
    assert response.status_code == 200, response.text
    return response.json()


def test_five_uploads_persist_and_review_with_consistent_scorecard(client):
    token = _login(client)
    files = [("연구계획서.txt", "연구 제목: 진료기록 후향적 연구\n연구 방법: 후향적 관찰 연구".encode()),
             ("data.csv", b"year,count\n2026,10"), ("scan.pdf", make_pdf()), ("신청서.docx", docx_bytes()),
             ("image.png", b"\x89PNG\r\n\x1a\n")]
    uploaded = _upload(client, token, *files)
    assert [(row["name"], row["status"]) for row in uploaded] == [(name, "ok") for name, _ in files]
    refreshed = client.get("/mvp").text
    assert all(name in refreshed for name, _ in files)
    assert 'class="sidebar"' not in refreshed and 'aria-current="step"' in refreshed

    started = client.post("/mvp/review", data={"csrf_token": token}, follow_redirects=False)
    assert started.status_code == 303
    review_url = started.headers["location"]
    result = client.get(f"/api{review_url}").json()
    review = client.get(review_url)
    assert result == {"status": "done", "stage": "채점 완료", "score": int(re.search(r'<strong>(\d+)</strong>', review.text)[1]),
                      "verdict": "FAIL"}
    assert review.text.count("<meter ") == 5 and "AI 심층 분석 없이 채점됨" in review.text
    scorecard = client.get(f"{review_url}/scores")
    assert scorecard.status_code == 200 and re.search(rf'<td>{result["score"]}</td>\s*<td data-severity="critical"', scorecard.text)
    assert 'data-view-panel="chart"' in scorecard.text and 'id="finding-doc"' in scorecard.text
    rules = client.get(f"{review_url}/scores?tab=criteria")
    assert rules.status_code == 200
    for name in ("critical", "major", "minor"):
        assert f"{name.title()} · {criteria()['scoring'][name]}점" in rules.text


def test_only_pending_owner_can_delete_and_binary_is_removed(client, db):
    token = _login(client)
    row, = _upload(client, token, ("report.txt", b"Research report"))
    path = Path(db.get(MvpUpload, row["id"]).path)
    assert path.is_file()
    admin_token = _login(client, "admin")
    route = f"/mvp/files/{row['id']}/delete"
    assert client.post(route, data={"csrf_token": admin_token}).status_code == 404
    assert path.is_file()

    token = _login(client)
    assert client.post(route, data={"csrf_token": token}, follow_redirects=False).status_code == 303
    assert not path.exists()
    assert row["name"] not in client.get("/mvp").text


def test_final_review_withdraws_cited_rule_error_and_keeps_real_mismatch_on_rerun(client, settings, db, monkeypatch):
    token = _login(client)
    plan = "연구명 | 성인 수면 연구\n목표 규모는 80명이다.\n"
    period = "수행 계획 | 2027년 1월 1일부터 2027년 6월 30일까지"
    application = f"연구명 | 성인 수면 연구\n규모 | 90명\n{period}\n"
    _upload(client, token, ("연구계획서.txt", plan.encode()), ("심의신청서.txt", application.encode()))
    url = client.post("/mvp/review", data={"csrf_token": token}, follow_redirects=False).headers["location"]
    run_id = int(url.rsplit("/", 1)[1])
    first = db.get(QCRun, run_id)
    assert any(issue["code"] == "REQ_APPLICATION_PERIOD" and str(issue["id"]) in first.counts["deductions"]
               for issue in first.issues)

    settings.ai_review_enabled = True
    settings.ai_provider = "openrouter"
    settings.openrouter_api_key = "test-key"

    def review(**request):
        if request["purpose"] == "서류 품질 점검":
            return {"issues": [], "summary": ""}
        reviews = []
        for line in request["content"][-1]["text"].splitlines():
            match = re.match(r"- index (\d+) \|", line)
            if not match:
                continue
            if "필수 기재사항 누락: 연구 기간 | 문서 번호: [1]" in line:
                reviews.append({"index": int(match[1]), "verdict": "false_positive", "reason": "신청서에 전체 기간이 있습니다.",
                                "locations": [{"doc_index": 1, "quote": period}]})
            elif "연구대상자 수 불일치" in line:
                reviews.append({"index": int(match[1]), "verdict": "false_positive", "reason": "인원은 같습니다.",
                                "locations": [{"doc_index": 0, "quote": plan.strip()},
                                              {"doc_index": 1, "quote": application.strip()}]})
            else:
                reviews.append({"index": int(match[1]), "verdict": "valid", "reason": "규칙 지적을 확인했습니다.", "locations": []})
        return {"reviews": reviews}

    monkeypatch.setattr(ai_client, "structured_call", review)
    retry = client.post(f"{url}/retry", data={"csrf_token": token}, follow_redirects=False)
    assert retry.status_code == 303 and retry.headers["location"] == url
    db.refresh(first)
    withdrawn, = [issue for issue in first.issues if issue["code"] == "REQ_APPLICATION_PERIOD"]
    mismatch, = [issue for issue in first.issues if issue["code"] == "CROSS_SUBJECTS"]
    assert withdrawn["review"] == "dismissed" and str(withdrawn["id"]) not in first.counts["deductions"]
    assert str(mismatch["id"]) in first.counts["deductions"] and mismatch["ai_review"]["verdict"] == "uncertain"
    assert len(mismatch["ai_review"]["locations"]) == 2
    scorecard = client.get(f"{url}/scores").text
    assert "AI 재검토로 철회한 지적" in scorecard and "신청서에 전체 기간이 있습니다." in scorecard


def test_rejected_uploads_cannot_start_review(client, settings):
    settings.max_upload_mb = 1
    token = _login(client)
    invalid = _upload(client, token, ("bundle.zip", b"zip"), ("data.txt", b"a" * (1024 * 1024 + 1)))
    assert [(item["status"], "ZIP" in item["message"], "초과" in item["message"]) for item in invalid] == [
        ("rejected", True, False), ("rejected", False, True),
    ]
    denied = client.post("/mvp/review", data={"csrf_token": token}, follow_redirects=False)
    assert denied.status_code == 303 and denied.headers["location"] == "/mvp"
    cleared = client.post("/mvp/files/clear", data={"csrf_token": token})
    assert all(item["name"] not in cleared.text for item in invalid)


def test_unreadable_document_has_no_score_and_is_private(client):
    token = _login(client)
    blank = PdfWriter()
    blank.add_blank_page(width=400, height=600)
    with io.BytesIO() as buffer:
        blank.write(buffer)
        row, = _upload(client, token, ("scan.pdf", buffer.getvalue()))
    assert row["status"] == "ok" and "본문을 읽지 못함" in row["message"]
    run = client.post("/mvp/review", data={"csrf_token": token}, follow_redirects=False).headers["location"]
    assert client.get(f"/api{run}").json()["score"] is None
    assert "채점 불가" in client.get(run).text

    _login(client, "admin")
    assert client.get(run).status_code == 404
    assert client.get(f"/api{run}").status_code == 404
    assert client.get(f"{run}/scores").status_code == 404


def test_ai_provider_failure_still_returns_rule_score(client, settings, monkeypatch, db):
    settings.ai_review_enabled = True
    settings.ai_provider = "openrouter"
    settings.openrouter_api_key = "test-key"

    def unavailable(**_request):
        raise ai_client.AIUnavailable("Test provider unavailable")

    monkeypatch.setattr(ai_client, "structured_call", unavailable)
    token = _login(client)
    _upload(client, token, ("memo.txt", b"Synthetic study memo for deterministic rule scoring."))
    review = client.post("/mvp/review", data={"csrf_token": token}, follow_redirects=False).headers["location"]

    result = client.get(f"/api{review}").json()
    assert result["status"] == "done" and isinstance(result["score"], int)
    assert "AI 심층 분석 없이 채점됨" in client.get(review).text
    assert db.get(QCRun, int(review.rsplit("/", 1)[1])).phases["2"]["status"] == "failed"


def test_stalled_review_becomes_failed_and_can_retry_same_files(client, db, users):
    token = _login(client)
    upload, = _upload(client, token, ("report.txt", b"Research report"))
    run = QCRun(created_by=users["researcher"].id, status="running", mvp_attempt="prior-attempt",
                stage_updated_at=now() - timedelta(minutes=11))
    db.add(run)
    db.flush()
    db.get(MvpUpload, upload["id"]).run_id = run.id
    db.commit()
    url = f"/mvp/review/{run.id}"
    assert client.get(f"/api{url}").json()["status"] == "failed"

    retry = client.post(f"{url}/retry", data={"csrf_token": token}, follow_redirects=False)
    assert retry.headers["location"] == url
    assert client.get(f"/api{url}").json()["status"] == "done"
    db.expire_all()
    assert db.scalar(select(MvpUpload.run_id).where(MvpUpload.id == upload["id"])) == run.id


def test_scoring_caps_area_and_counts_verified_highest_severity_once():
    def issue(i: int, category: str, severity: str, **values):
        return {"id": i, "category": category, "severity": severity, "doc": "d1", **values}

    run = QCRun(docs=[{"key": "d1", "chars": 100}], issues=[
        issue(1, "version", "minor", group="shared"),
        issue(2, "version", "critical", group="shared"),
        issue(3, "numbers", "major", review="confirmed"),
        issue(4, "cross", "critical", confidence="ai_unverified"),
        issue(5, "typo", "minor", confidence="ai_verified"),
        issue(6, "placeholder", "major", result="needs_check"),
        issue(7, "regulatory", "critical", advice=True),
        issue(8, "required", "critical", merged_into=2),
    ])
    score_run(run)
    areas = run.counts["areas"]
    assert [(area["score"], area["issue_ids"]) for area in areas.values()] == [
        (35, []), (25, []), (20, []), (0, [2, 3]), (8, [5]),
    ]
    assert run.score == sum(area["score"] for area in areas.values()) == 88
    assert run.counts["documents"]["d1"]["deduction"] == 12
    assert run.verdict == "FAIL"


@pytest.mark.parametrize(("name", "entries", "data"), [
    ("slides.pptx", {"[Content_Types].xml": "", "ppt/presentation.xml": ""}, b""),
    ("document.odt", {"mimetype": "application/vnd.oasis.opendocument.text", "content.xml": ""}, b""),
    ("document.rtf", {}, b"{\\rtf1 Sample document}"),
])
def test_office_formats_require_matching_signatures(name, entries, data):
    if entries:
        buffer = io.BytesIO()
        with ZipFile(buffer, "w") as archive:
            for filename, content in entries.items():
                archive.writestr(filename, content)
        data = buffer.getvalue()
    assert validate_upload(name, data).ok
    assert not validate_upload(name, b"not an office document").ok


def test_running_review_in_this_process_is_not_expired(client, db, users, monkeypatch):
    from irb_automation.mvp import service

    _login(client)
    run = QCRun(created_by=users["researcher"].id, status="running", mvp_attempt="live", stage="AI 심층 분석 중",
                stage_updated_at=now() - timedelta(minutes=30))
    db.add(run)
    db.commit()
    monkeypatch.setattr(service, "_running", {run.id})
    assert client.get(f"/api/mvp/review/{run.id}").json()["status"] == "running"
    monkeypatch.setattr(service, "_running", set())
    assert client.get(f"/api/mvp/review/{run.id}").json()["status"] == "failed"


def test_unscored_findings_are_listed_for_manual_check(client, settings, monkeypatch):
    settings.ai_review_enabled = True
    settings.ai_provider = "openrouter"
    settings.openrouter_api_key = "test-key"
    monkeypatch.setattr(ai_client, "structured_call", lambda **_request: {"summary": "검토 요약 문장", "issues": [
        {"doc_index": 0, "severity": "major", "category": "context", "title": "인용이 원문에 없는 지적",
         "rule_id": "M10", "flag": "서술 모순", "evidence": "문서에 없는 문장입니다.", "locations": []},
        {"doc_index": 0, "severity": "major", "category": "context", "title": "원문으로 확인된 지적",
         "rule_id": "M10", "flag": "서술 모순", "evidence": "대상자는 익명으로 처리한다.", "locations": []},
    ]})
    token = _login(client)
    _upload(client, token, ("memo.txt", "대상자는 익명으로 처리한다. 대상자의 등록번호를 수집한다.".encode()))
    review = client.post("/mvp/review", data={"csrf_token": token}, follow_redirects=False).headers["location"]

    page = client.get(review).text
    assert "AI 심층 분석 포함" in page and "AI 심층 분석 없이 채점됨" not in page
    scores = client.get(f"{review}/scores").text
    assert "확인 필요 항목" in scores and "인용이 원문에 없는 지적" in scores and "원문 인용 미확인" in scores
    assert "검토 요약 문장" in scores
    verified = re.search(r'data-status="deduction">(감점 \d+점)</span>\s*<strong>원문으로 확인된 지적</strong>\s*</div>\s*'
                         r'<p class="small muted">([^<]+)', scores)
    assert verified and "AI 지적" in verified[2]


def test_scorecard_separates_criteria_and_highlights_verified_pdf_lines(client, db, settings, monkeypatch):
    settings.ai_review_enabled = True
    settings.ai_provider = "openrouter"
    settings.openrouter_api_key = "test-key"
    sample_size = "All eligible patients are included. Target: 1,500."
    size_followup = "The number reflects the planned enrollment window."
    timing = "Eligibility applies within the study period."
    repeated = "Study protocol"
    pdf = io.BytesIO()
    document = Canvas(pdf)
    document.drawString(72, 760, repeated)
    document.showPage()
    document.drawString(72, 760, repeated)
    for y, line in ((740, sample_size), (724, size_followup)):
        x = 72
        for word in line.split():
            document.drawString(x, y, word)
            x += document.stringWidth(word + " ")
    document.drawString(72, 698, timing)
    document.save()
    monkeypatch.setattr(ai_client, "structured_call", lambda **_request: {"summary": "", "issues": [
        {"doc_index": 0, "severity": "major", "category": "context", "title": "대상자 수 산출 근거 미흡",
         "rule_id": "E02", "flag": "대상자 수", "detail": "목표 수와 전수 조사 기준의 관계를 설명해 주세요.",
         "evidence": f"{sample_size} {size_followup}"},
        {"doc_index": 0, "severity": "major", "category": "context", "title": "자료 수집 기간 표현 불명확",
         "rule_id": "M05", "flag": "기간 구분 오류", "detail": "자료 수집 기간과 연구 수행 기간을 구분해 주세요.",
         "evidence": timing},
        {"doc_index": 0, "severity": "minor", "category": "context", "title": "제목 구체화",
         "rule_id": "M10", "flag": "설명 불명확", "question": "제목이 연구 유형을 드러내는지 확인해 주세요.",
         "evidence": repeated},
    ]})
    token = _login(client)
    _upload(client, token, ("protocol.pdf", pdf.getvalue()))
    url = client.post("/mvp/review", data={"csrf_token": token}, follow_redirects=False).headers["location"]
    scores = client.get(f"{url}/scores").text
    size_card = re.search(r'<strong>대상자 수 산출 근거 미흡</strong>(.*?)</li>', scores, re.S)[1]
    time_card = re.search(r'<strong>자료 수집 기간 표현 불명확</strong>(.*?)</li>', scores, re.S)[1]
    assert "E02 · 대상자 수" in size_card and "· 2–3번째 줄" in size_card and sample_size in size_card
    assert "M05 · 기간 구분 오류" in time_card and "· 4번째 줄" in time_card and timing in time_card
    assert "2페이지" in size_card and "2페이지" in time_card
    notes = scores.split('id="notes-heading"', 1)[1].split("</section>", 1)[0]
    assert "제목이 연구 유형을 드러내는지 확인해 주세요." in notes
    assert "같은 문구가 여러 곳에 있어 위치를 특정할 수 없습니다." in notes and ".png" not in notes

    image_url = re.search(r'src="([^"]+\.png)\?preview=true"', size_card)[1]
    viewer_url = re.search(r'<a href="(/mvp/review/[^"]+/viewer\?[^"]+)" data-document-viewer target="irb-document-viewer"><img',
                           size_card)[1].replace("&amp;", "&")
    viewer = client.get(viewer_url)
    assert viewer.status_code == 200 and f'src="{image_url}"' in viewer.text and "대상자 수 산출 근거 미흡" in viewer.text
    assert "2페이지로 이동" in viewer.text
    document_url = re.search(r'href="([^"?]+/documents/d1)\?p=2#page=2"', viewer.text)[1]
    document = client.get(document_url)
    assert document.headers["content-type"] == "application/pdf" and document.headers["x-frame-options"] == "SAMEORIGIN"
    image_response = client.get(image_url)
    assert image_response.status_code == 200
    assert image_response.headers["cache-control"] == "private, no-store"
    preview_response = client.get(f"{image_url}?preview=true")
    with Image.open(io.BytesIO(image_response.content)) as image, Image.open(io.BytesIO(preview_response.content)) as preview:
        for baseline in (740, 724):
            row = image.height - 2 * (baseline + 4)
            reds = [x for x in range(image.width) if image.getpixel((x, row)) == (208, 0, 32)]
            assert len(reds) == 4 and min(reds) >= 135
        between = image.height - 2 * 736
        assert all(image.getpixel((x, between)) != (208, 0, 32) for x in range(image.width))
        colors = preview.getcolors(preview.width * preview.height)
        assert preview.height < image.height // 2 and any(color == (208, 0, 32) for _, color in colors)

    run = db.get(QCRun, int(url.rsplit("/", 1)[1]))
    run.counts = {key: value for key, value in run.counts.items() if key != "source_locations"}
    db.commit()
    assert "2–3번째 줄" in client.get(f"{url}/scores").text
    assert client.get(image_url).content == image_response.content

    _login(client, "admin")
    assert client.get(image_url).status_code == 404


def test_local_env_file_fills_only_unset_variables(tmp_path, monkeypatch):
    from irb_automation.config import load_env_file

    env = tmp_path / ".env"
    env.write_text("# comment\nIRB_AI_ENABLED=true\nOPENROUTER_API_KEY='file-key'\nIRB_AI_PROVIDER=openrouter\n", encoding="utf-8")
    import os

    fake = {"IRB_AI_PROVIDER": "anthropic"}
    monkeypatch.setattr(os, "environ", fake)
    assert load_env_file(env)
    assert fake == {"IRB_AI_ENABLED": "true", "OPENROUTER_API_KEY": "file-key", "IRB_AI_PROVIDER": "anthropic"}
    assert not load_env_file(tmp_path / "missing.env")
