from __future__ import annotations

import html
import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from irb_automation.ai import client as ai
from irb_automation.database import init_db
from irb_automation.models import Role, User
from irb_automation.qc.models import QCRuleFeedback, QCRuleKeyword, QCRun
from irb_automation.security import verify_password
from irb_automation.seed import ensure_super_admin

from .conftest import PASSWORD
from .test_mvp import _token, _upload

BOOTSTRAP_PASSWORD = "Initial!Pass2026"


def _sign_in(client: TestClient, username: str, password: str) -> str:
    client.cookies.clear()
    token = _token(client.get("/login").text)
    response = client.post("/login", data={"username": username, "password": password, "csrf_token": token},
                           follow_redirects=False)
    return response.headers["location"]


def test_configured_super_admin_is_created_once_and_web_roles_cannot_escalate(app, db, settings) -> None:
    settings.super_admin_password = BOOTSTRAP_PASSWORD
    with TestClient(app) as client:
        owner = db.scalar(select(User).where(User.username == "super_admin"))
        assert owner.role == Role.SUPER_ADMIN and verify_password(BOOTSTRAP_PASSWORD, owner.password_hash)
        old_hash = owner.password_hash
        assert not ensure_super_admin(db, "Different!Pass2027") and owner.password_hash == old_hash
        assert client.get("/super-admin", follow_redirects=False).headers["location"] == "/login"

        _sign_in(client, "admin", PASSWORD)
        page = client.get("/admin/users")
        token = _token(page.text)
        for url in ("/super-admin", "/super-admin/cases", "/super-admin/rules", "/super-admin/cases/1"):
            assert client.get(url).status_code == 403
        assert client.post("/super-admin/cases/1", data={"csrf_token": token, "decision": "reviewed", "note": "test"}).status_code == 403
        update = {"csrf_token": token, "role": "researcher", "password": "Modified!2026"}
        assert client.post(f"/admin/users/{owner.id}", data=update).status_code == 403
        create = {**update, "username": "elevated", "role": "super_admin"}
        assert client.post("/admin/users", data=create).status_code == 403
        assert client.post("/admin/users", data={**create, "username": "super_admin", "role": "admin"}).status_code == 403

        assert _sign_in(client, "super_admin", BOOTSTRAP_PASSWORD) == "/super-admin"
        response = client.get("/super-admin")
        assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
        assert client.get("/super-admin/rules").status_code == 200


def test_verified_rule_dispute_can_be_approved_and_used_in_the_next_review(app, db, settings, monkeypatch) -> None:
    settings.mvp_mode = settings.pipeline_sync = settings.ai_review_enabled = True
    settings.ai_provider = "openrouter"
    settings.openrouter_api_key = "local-test-stub"
    settings.super_admin_password = BOOTSTRAP_PASSWORD
    criterion = "REQ_PROTOCOL_SAMPLE_SIZE"
    phrase = "예상 탈락"
    evidence = "목표 규모는 80명이다. 이전 응답률과 예상 탈락 20%를 반영하였다."
    text = f"# 1. 연구 제목\n연구명 | 업무 피로 연구\n# 2. 배경\n피로의 변화를 조사한다.\n# 8. 참여 인원 설계\n{evidence}"

    def review(**request):
        if request["purpose"] == "서류 품질 점검":
            return {"summary": "", "issues": []}
        reviewed = []
        for line in request["content"][-1]["text"].splitlines():
            match = re.match(r"- index (\d+) \|", line)
            if match:
                disputed = "대상자 수 및 산출 근거" in line
                reviewed.append({"index": int(match[1]), "verdict": "false_positive" if disputed else "valid",
                                 "reason": "응답률과 탈락률을 기재했습니다.",
                                 "locations": [{"doc_index": 0, "quote": evidence}] if disputed else []})
        return {"reviews": reviewed}

    monkeypatch.setattr(ai, "structured_call", review)
    with TestClient(app) as client:
        assert _sign_in(client, "researcher", PASSWORD) == "/mvp"
        token = _token(client.get("/mvp").text)
        contents = text.encode()
        _upload(client, token, ("연구계획서.txt", contents))
        start = client.post("/mvp/review", data={"csrf_token": token}, follow_redirects=False)
        url = start.headers["location"]
        client.post(f"{url}/retry", data={"csrf_token": token}, follow_redirects=False)
        case, = db.scalars(select(QCRuleFeedback).where(QCRuleFeedback.rule_code == criterion)).all()
        assert case.status == "pending" and case.outcome == "dismissed"
        assert case.citations == [{"doc": "d1", "doc_type": "protocol", "quote": evidence}]
        assert client.get(f"/super-admin/cases/{case.id}").status_code == 403

        _sign_in(client, "super_admin", BOOTSTRAP_PASSWORD)
        assert client.get(f"/super-admin/cases?q={criterion}").status_code == 200
        case_url = f"/super-admin/cases/{case.id}"
        body = client.get(case_url).text
        condition = html.unescape(re.search(r'<option value="([^"]+)">[^<]*산출', body)[1])
        decision = {"decision": "applied", "keyword": phrase, "condition": condition, "note": "표본 근거 표현 추가"}
        assert client.post(case_url, data=decision).status_code == 403
        assert client.post(case_url, data={**decision, "csrf_token": _token(body)}, follow_redirects=False).status_code == 303
        db.refresh(case)
        alias, = db.scalars(select(QCRuleKeyword)).all()
        assert case.status == "applied" and alias.keyword == phrase and alias.is_active
        stats = client.get("/super-admin?q=researcher").text
        assert f"{len(contents):,} B" in stats and "researcher" in stats

        settings.ai_review_enabled = False
        _sign_in(client, "researcher", PASSWORD)
        token = _token(client.get("/mvp").text)
        client.post(f"{url}/retry", data={"csrf_token": token}, follow_redirects=False)
        run = db.get(QCRun, case.run_id)
        assert not any(issue["code"] == criterion for issue in run.issues)
        assert db.get(QCRuleFeedback, case.id).status == "applied"


def test_filename_rule_can_be_registered_disabled_and_restored_from_history(app, settings) -> None:
    settings.super_admin_password = BOOTSTRAP_PASSWORD
    rules = "/super-admin/rules"
    filename = {"name": "QA-BLUE-482.txt"}
    pattern = {"scope": "filename", "doc_type": "consent_form", "format": "regex", "expression": r"^QA-BLUE-\d+$"}
    with TestClient(app) as client:
        _sign_in(client, "admin", PASSWORD)
        token = _token(client.get("/admin/users").text)
        assert client.post(f"{rules}/patterns", data={**pattern, "csrf_token": token}).status_code == 403

        _sign_in(client, "super_admin", BOOTSTRAP_PASSWORD)
        token = _token(client.get(rules).text)
        assert client.post(f"{rules}/patterns", data=pattern).status_code == 403
        invalid = client.post(f"{rules}/patterns", data={**pattern, "expression": "(", "csrf_token": token})
        assert "정규표현식 문법을 확인" in invalid.text
        assert client.get("/documents/guess", params=filename).json()["doc_type"] == "other"

        response = client.post(f"{rules}/patterns", data={**pattern, "csrf_token": token})
        assert response.status_code == 200
        assert client.get("/documents/guess", params=filename).json()["doc_type"] == "consent_form"
        selected = client.get(rules, params={"doc_type": "consent_form", "q": "QA-BLUE"}).text
        elsewhere = client.get(rules, params={"doc_type": "protocol", "q": "QA-BLUE"}).text
        assert 'id="class-consent_form" open' in selected
        assert 'id="class-consent_form"' not in elsewhere and '/history/' not in elsewhere
        button = re.search(r'<code>\^QA-BLUE[^<]+</code>.*?action="(/super-admin/rules/patterns/\d+)"', selected, re.S)
        assert button

        disabled = client.post(button[1], data={"active": "no", "csrf_token": token})
        init_db()
        assert client.get("/documents/guess", params=filename).json()["doc_type"] == "other"
        restore = re.search(r'action="(/super-admin/rules/history/\d+/restore)"', disabled.text)
        assert restore
        assert client.post(restore[1], data={}).status_code == 403
        restored = client.post(restore[1], data={"csrf_token": token})
        assert client.get("/documents/guess", params=filename).json()["doc_type"] == "consent_form"
        assert len(re.findall(r'action="/super-admin/rules/history/\d+/restore"', restored.text)) == 3


def test_added_required_regex_is_loaded_on_the_next_document_review(app, db, settings) -> None:
    settings.mvp_mode = settings.pipeline_sync = True
    settings.ai_review_enabled = False
    settings.super_admin_password = BOOTSTRAP_PASSWORD
    code = "REQ_PROTOCOL_SAMPLE_SIZE"
    text = "# 1. 연구 제목\n연구명 | 업무 피로 연구\n# 2. 배경\n연구는 피로의 변화를 조사한다.\n"
    text += "# 8. 참여 인원 설계\n목표 규모는 80명이다. 이전 응답률과 예상 탈락률 20%를 반영하였다."
    rules = "/super-admin/rules"

    with TestClient(app) as client:
        _sign_in(client, "researcher", PASSWORD)
        token = _token(client.get("/mvp").text)
        _upload(client, token, ("연구계획서.txt", text.encode()))
        start = client.post("/mvp/review", data={"csrf_token": token}, follow_redirects=False)
        url = start.headers["location"]
        run = db.get(QCRun, int(url.rsplit("/", 1)[-1]))
        assert any(issue["code"] == code for issue in run.issues)

        _sign_in(client, "super_admin", BOOTSTRAP_PASSWORD)
        token = _token(client.get(rules).text)
        response = client.post(f"{rules}/patterns", data={
            "scope": "required", "doc_type": "protocol", "rule_code": code, "format": "regex",
            "expression": r"예상 탈락률\s*\d+%", "condition": '["all", "re:산출|근거|검정력|전수"]', "csrf_token": token,
        }, follow_redirects=False)
        assert response.status_code == 303

        _sign_in(client, "researcher", PASSWORD)
        token = _token(client.get("/mvp").text)
        client.post(f"{url}/retry", data={"csrf_token": token}, follow_redirects=False)
        db.refresh(run)
        assert all(issue["code"] != code for issue in run.issues)


def test_added_body_keyword_changes_document_type_on_upload(app, settings) -> None:
    settings.mvp_mode = True
    settings.super_admin_password = BOOTSTRAP_PASSWORD
    rules = "/super-admin/rules"
    file = "synthetic.txt", "옥수수 색상 비교\n녹색과 노란색 중 한 가지를 골라 주세요.".encode()

    with TestClient(app) as client:
        _sign_in(client, "researcher", PASSWORD)
        token = _token(client.get("/mvp").text)
        before, = _upload(client, token, file)
        assert before["doc_type_label"] != "설문지·면담질문지"

        _sign_in(client, "super_admin", BOOTSTRAP_PASSWORD)
        token = _token(client.get(rules).text)
        response = client.post(f"{rules}/patterns", data={
            "scope": "body", "doc_type": "questionnaire", "format": "literal",
            "expression": "옥수수 색상 비교", "weight": "8", "csrf_token": token,
        }, follow_redirects=False)
        assert response.status_code == 303

        _sign_in(client, "researcher", PASSWORD)
        token = _token(client.get("/mvp").text)
        after, = _upload(client, token, file)
        assert after["doc_type_label"] == "설문지·면담질문지"
