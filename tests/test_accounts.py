"""기본 계정 자동 생성과 계정별 AI 등급."""
from __future__ import annotations

import re

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import select

from irb_automation.ai import tier
from irb_automation.models import Role, User
from irb_automation.security import verify_password


def _login(client: TestClient, username: str, password: str) -> int:
    client.cookies.clear()
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/login").text).group(1)
    return client.post("/login", data={"username": username, "password": password, "csrf_token": token},
                       follow_redirects=False).status_code


def test_builtin_accounts_are_created_once_and_passwords_follow_settings(db, settings) -> None:
    from irb_automation.seed import ensure_builtin_accounts

    assert ensure_builtin_accounts(db) == ["guest 생성", "admin 생성"]
    db.commit()
    guest = db.scalars(select(User).where(User.username == "guest")).one()
    admin = db.scalars(select(User).where(User.username == "admin")).one()
    assert (guest.role, admin.role) == (Role.RESEARCHER, Role.ADMIN)
    assert verify_password("guest", guest.password_hash) and verify_password("medilux_admin", admin.password_hash)

    assert ensure_builtin_accounts(db) == []  # 이미 있으면 건드리지 않는다

    settings.admin_password = "changed-by-env"
    admin.is_active = False
    assert ensure_builtin_accounts(db) == ["admin 비밀번호 재설정", "admin 역할·활성 복구"]
    assert verify_password("changed-by-env", admin.password_hash) and admin.is_active
    assert db.scalars(select(User)).all().__len__() == 2


def test_existing_demo_admin_gets_the_configured_password(db, users) -> None:
    from irb_automation.seed import ensure_builtin_accounts

    before = users["admin"].id
    assert ensure_builtin_accounts(db) == ["guest 생성", "admin 비밀번호 재설정"]
    admin = db.scalars(select(User).where(User.username == "admin")).one()
    assert admin.id == before and verify_password("medilux_admin", admin.password_hash)


def test_server_start_adds_missing_accounts_and_login_is_required(settings) -> None:
    from irb_automation.main import create_app

    settings.builtin_accounts = True
    settings.super_admin_password = ""
    with TestClient(create_app()) as client:
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"
        assert "guest / guest" in client.get("/login").text
        assert _login(client, "guest", "wrong") == 303 and client.get("/", follow_redirects=False).status_code == 303
        assert _login(client, "guest", "guest") == 303
        assert client.get("/", follow_redirects=False).status_code == 200
        assert _login(client, "admin", "medilux_admin") == 303
        assert client.get("/", follow_redirects=False).status_code == 200
        assert _login(client, "super_admin", "medilux_super_admin") == 303
        assert client.get("/super-admin", follow_redirects=False).status_code == 200


def test_ai_provider_and_model_follow_the_logged_in_account(settings) -> None:
    from irb_automation.main import create_app

    settings.builtin_accounts = True
    settings.ai_provider, settings.ai_model = "openrouter", "ignored-for-admin"
    app = create_app()

    @app.get("/_tier")
    def seen(request: Request) -> dict:  # 동기 핸들러(스레드풀)에서도 등급이 보여야 한다
        return {"tier": tier.current(), "provider": tier.provider(), "model": tier.model(),
                "background": tier.carry(tier.provider)()}

    with TestClient(app) as client:
        assert client.get("/_tier").json()["tier"] is None  # 로그인 전: 환경 설정 그대로
        _login(client, "guest", "guest")
        assert client.get("/_tier").json() == {"tier": "free", "provider": "openrouter", "model": "ignored-for-admin",
                                               "background": "openrouter"}
        _login(client, "admin", "medilux_admin")
        assert client.get("/_tier").json() == {"tier": "claude", "provider": "anthropic", "model": "claude-sonnet-5-5",
                                               "background": "anthropic"}
    assert tier.current() is None  # 요청이 끝나면 등급이 새지 않는다


def test_claude_tier_needs_its_own_key_and_sends_the_admin_model(settings) -> None:
    from irb_automation.ai import client

    settings.ai_review_enabled, settings.ai_provider = True, "openrouter"
    settings.openrouter_api_key, settings.claude_api_key = "free-key", ""
    with tier.use(tier.FREE):
        assert client.ai_enabled()
    with tier.use(tier.CLAUDE):
        assert not client.ai_enabled()  # 무료 키만으로 유료 등급이 켜지지 않는다

    sent = {}

    class Messages:
        def create(self, **kwargs):
            sent.update(kwargs)
            return type("R", (), {"stop_reason": "end_turn", "content": [], "usage": None, "model": kwargs["model"]})()

    fake = type("C", (), {"beta": type("B", (), {"messages": Messages()})()})()
    client.set_client_factory(lambda: fake)
    try:
        with tier.use(tier.CLAUDE):
            assert client.ai_enabled()
            client.create_message(system="s", messages=[{"role": "user", "content": "c"}], max_tokens=100)
    finally:
        client.set_client_factory(None)
    assert sent["model"] == "claude-sonnet-5-5"
