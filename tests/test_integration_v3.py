"""v3 통합: 웹 업로드 → 제출 hook → AI 문서 파이프라인(관련 정보 검색 포함), 자동 작성 문서 버전 관리."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from irb_automation.documents import generate_and_attach
from irb_automation.models import Document, Protocol
from irb_automation.pipeline.models import PipelineRun

from .conftest import csrf, login

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "samples"))
import generate_documents as samples  # noqa: E402


def _protocol(db, users) -> Protocol:
    p = Protocol(title="통합 검증 과제", pi_id=users["researcher"].id, department="내분비내과",
                 data_sources=["emr"], deid_level="pseudonymized", consent_plan="waiver", expected_subjects=100)
    db.add(p)
    db.commit()
    return p


def test_web_upload_triggers_pipeline_with_related_search(app, db, users, settings):
    settings.pipeline_sync = True
    pid = _protocol(db, users).id
    with TestClient(app) as c:
        login(c, "researcher")
        t = csrf(c)
        r = c.post(f"/protocols/{pid}/documents", data={"csrf_token": t, "doc_type": "protocol"},
                   files={"file": ("연구계획서.docx", samples.build_docx(), "application/octet-stream")})
        assert r.status_code in (200, 303)
    db.expire_all()
    doc = db.scalars(select(Document).where(Document.protocol_id == pid)).one()
    runs = db.scalars(select(PipelineRun).where(PipelineRun.protocol_id == pid)).all()
    assert runs, "업로드 후 문서 분석 파이프라인이 자동 실행되어야 합니다"
    run = runs[-1]
    steps = {s.name: s.status for s in run.steps}
    assert steps.get("related") == "succeeded", steps
    assert doc.is_current


def test_generated_documents_are_versioned(db, users):
    p = _protocol(db, users)
    first = generate_and_attach(db, users["researcher"], p, "application")
    second = generate_and_attach(db, users["researcher"], p, "application")
    db.flush()
    db.refresh(first)
    assert second.version == first.version + 1
    assert second.is_current and not first.is_current and first.status == "superseded"
    assert second.source == "generated"
