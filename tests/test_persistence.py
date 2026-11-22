"""재배포(로컬 디스크 초기화) 뒤에도 업로드 원본·심사 결과·비밀값이 DB에서 복원되는지 확인한다.

기본은 SQLite에 IRB_FILE_STORE=db를 켜서 검사하고, IRB_TEST_DATABASE_URL을 주면 PostgreSQL에서 검사한다.
"""
from __future__ import annotations

import re
import shutil

import pytest
from fastapi.testclient import TestClient

from irb_automation import filestore
from irb_automation.config import Settings, normalize_database_url, resolve_database_url
from irb_automation.database import SessionLocal, sync_secrets
from irb_automation.mvp.models import MvpUpload

from .conftest import make_pdf
from .test_mvp import _login, _upload


@pytest.fixture
def client(app, settings, monkeypatch):
    monkeypatch.setenv("IRB_FILE_STORE", "db")
    settings.mvp_mode = True
    settings.pipeline_sync = True
    settings.ai_review_enabled = False
    with TestClient(app) as client:
        yield client


def test_uploads_and_viewer_survive_wiped_disk(client, settings):
    token = _login(client)
    row, = _upload(client, token, ("protocol.pdf", make_pdf(2, "Sample size: 100 subjects.")))
    with SessionLocal() as db:
        path = db.get(MvpUpload, row["id"]).path
        stored = db.get(filestore.StoredFile, filestore.key_for(path))
        assert stored is not None and stored.size > 0
    url = client.post("/mvp/review", data={"csrf_token": token}, follow_redirects=False).headers["location"]
    assert client.get(f"/api{url}").json()["status"] == "done"

    # 재배포: 컨테이너의 로컬 데이터 디렉터리가 비워진 상황
    shutil.rmtree(settings.data_dir / "mvp")
    viewer = client.get(f"{url}/viewer")
    assert viewer.status_code == 200 and "protocol.pdf" in viewer.text
    document_url = re.search(r'src="(/mvp/review/\d+/documents/d1)"', viewer.text)[1]
    document = client.get(document_url)
    assert document.status_code == 200 and document.content.startswith(b"%PDF")
    assert (settings.data_dir / "mvp").is_dir()  # DB에서 꺼내 디스크 캐시로 다시 씀


def test_pending_upload_removal_deletes_stored_copy(client):
    token = _login(client)
    row, = _upload(client, token, ("memo.txt", b"Research memo"))
    with SessionLocal() as db:
        key = filestore.key_for(db.get(MvpUpload, row["id"]).path)
    assert client.post(f"/mvp/files/{row['id']}/delete", data={"csrf_token": token},
                       follow_redirects=False).status_code == 303
    with SessionLocal() as db:
        assert db.get(filestore.StoredFile, key) is None


def test_generated_secrets_are_shared_across_restarts(settings):
    if settings.is_sqlite:
        pytest.skip("SQLite는 로컬 키 파일을 사용한다")
    first = Settings(data_dir=settings.data_dir, database_url=settings.database_url)
    sync_secrets(first)
    second = Settings(data_dir=settings.data_dir, database_url=settings.database_url)
    assert second.secret_key != first.secret_key  # 재시작 직후 무작위 값
    sync_secrets(second)
    assert (second.secret_key, second.pseudonym_secret) == (first.secret_key, first.pseudonym_secret)


def test_vercel_postgres_urls_are_normalized(monkeypatch, tmp_path):
    for name in ("IRB_DATABASE_URL", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("POSTGRES_URL", "postgres://u:p@host/db?sslmode=require")
    assert normalize_database_url(resolve_database_url(tmp_path)) == "postgresql+psycopg://u:p@host/db?sslmode=require"
    monkeypatch.setenv("DATABASE_URL", "postgresql://a:b@neon/db")
    assert normalize_database_url(resolve_database_url(tmp_path)) == "postgresql+psycopg://a:b@neon/db"
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"
