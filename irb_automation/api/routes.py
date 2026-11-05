"""REST API - 병원 내부 시스템(CDW·데이터 추출 시스템·EMR) 연동용.

- 세션 로그인 사용자: 과제 조회, 무상태 사전검토 평가
- 연동 토큰(X-IRB-Token, 환경변수 IRB_INTEGRATION_TOKEN): 과제 승인 상태 확인
  → CDW에서 데이터 추출 요청 시 "유효한 IRB 승인이 있는가"를 자동 확인하는 게이트로 사용
"""
from __future__ import annotations

import os
import secrets
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import __version__
from ..database import get_db
from ..deid.detector import analyze_table
from ..deid.pipeline import read_csv_bytes
from ..engine.facts import ProtocolFacts
from ..engine.prereview import run_prereview
from ..models import Protocol, Role, User
from ..web.deps import current_user, require_roles
from ..workflow.service import can_view

router = APIRouter(prefix="/api", tags=["api"])


class FactsIn(BaseModel):
    """무상태 사전검토 입력. 필드 의미는 engine.facts.ProtocolFacts 참조."""

    title: str = ""
    study_type: str = "retrospective_chart_review"
    objectives: str = ""
    methods: str = ""
    inclusion_criteria: str = ""
    expected_subjects: int = 0
    sample_size_justification: str = ""
    existing_data_only: bool = True
    direct_contact: bool = False
    data_period_start: date | None = None
    data_period_end: date | None = None
    data_items: str = ""
    identifiers_collected: list[str] = Field(default_factory=list)
    identifier_justification: str = ""
    sensitive_categories: list[str] = Field(default_factory=list)
    vulnerable_populations: list[str] = Field(default_factory=list)
    unstructured_data: list[str] = Field(default_factory=list)
    rare_disease: bool = False
    deid_level: str = "pseudonymized"
    deid_performed_by: str = "data_steward"
    storage_location: str = "internal_secure"
    access_controls: str = ""
    external_transfer: bool = False
    external_recipient: str = ""
    international_transfer: bool = False
    data_linkage: bool = False
    linkage_target: str = ""
    retention_years: int = 3
    destruction_method: str = ""
    ai_ml_development: bool = False
    commercial_use: bool = False
    consent_plan: str = "waiver"
    waiver_impracticable_reasons: list[str] = Field(default_factory=list)
    waiver_impracticable_detail: str = ""
    waiver_no_presumed_refusal: bool = False
    conflict_of_interest: bool = False
    coi_detail: str = ""
    funding_source: str = ""
    submission_kind: str = "initial"
    uploaded_doc_types: list[str] = Field(default_factory=list)


@router.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@router.post("/prereview/evaluate")
def evaluate(body: FactsIn, user: User = Depends(current_user)) -> dict[str, Any]:
    """저장 없이 사전검토 규칙 엔진만 실행(연구계획 시뮬레이션)."""
    facts = ProtocolFacts.from_mapping(body.model_dump(), pi_training_expires_on=user.training_expires_on)
    return run_prereview(facts)


@router.get("/protocols")
def protocols(user: User = Depends(current_user), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    out = []
    for p in db.scalars(select(Protocol).order_by(Protocol.id.desc())):
        if not can_view(db, user, p):
            continue
        out.append(_summary(p))
    return out


@router.get("/protocols/{pid}/prereview")
def protocol_prereview(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    p = db.get(Protocol, pid)
    if p is None or not can_view(db, user, p):
        raise HTTPException(404)
    sub = p.current_submission
    if sub is None or sub.latest_prereview is None:
        raise HTTPException(404, "제출된 신청건이 없습니다.")
    return sub.latest_prereview.result


@router.post("/deid/analyze")
async def deid_analyze(
    file: UploadFile = File(...), user: User = Depends(require_roles(Role.DATA_STEWARD))
) -> dict[str, Any]:
    try:
        headers, rows, enc = read_csv_bytes(await file.read())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    result = analyze_table(headers, rows)
    result["encoding"] = enc
    return result


def _integration_auth(x_irb_token: str | None = Header(default=None)) -> None:
    expected = os.environ.get("IRB_INTEGRATION_TOKEN", "")
    if not expected:
        raise HTTPException(503, "연동 토큰이 설정되지 않았습니다(IRB_INTEGRATION_TOKEN).")
    if not x_irb_token or not secrets.compare_digest(x_irb_token, expected):
        raise HTTPException(401, "유효하지 않은 연동 토큰")


@router.get("/integration/approval/{protocol_no}", dependencies=[Depends(_integration_auth)])
def approval_status(protocol_no: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    """CDW/데이터 추출 시스템용 승인 확인. 유효 승인 여부와 허용 범위를 반환한다."""
    p = db.scalars(select(Protocol).where(Protocol.protocol_no == protocol_no)).first()
    if p is None:
        raise HTTPException(404, "과제번호를 찾을 수 없습니다.")
    return {
        **_summary(p),
        "data_scope": {
            "data_sources": p.data_sources,
            "data_period_start": p.data_period_start,
            "data_period_end": p.data_period_end,
            "deid_level": p.deid_level,
            "identifiers_allowed": p.identifiers_collected,
            "expected_subjects": p.expected_subjects,
            "data_items": [x.strip() for x in p.data_items.splitlines() if x.strip()],
        },
        "members": sorted(p.member_ids()),
    }


def _summary(p: Protocol) -> dict[str, Any]:
    return {
        "id": p.id,
        "protocol_no": p.protocol_no,
        "title": p.title,
        "pi": p.pi.full_name if p.pi else None,
        "status": p.status.value,
        "status_label": p.status.label,
        "review_type": p.review_type.value if p.review_type else None,
        "risk_score": p.risk_score,
        "approved_at": p.approved_at,
        "expires_at": p.expires_at,
        "active_approval": p.is_active_approval,
    }
