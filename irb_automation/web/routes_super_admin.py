"""Highest administrator access to service totals, rule criteria, and review decisions."""
from __future__ import annotations

import json
from typing import Annotated, Literal

import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..docproc.classify import keywords as classification_keywords
from ..models import Role, User
from ..qc.category_scores import VERDICTS
from ..qc.checks import criteria
from ..qc.feedback import FeedbackConflict, keyword_targets, required_criteria, review_case, set_keyword_active
from ..qc.models import QCRuleFeedback, QCRuleKeyword
from ..qc.rules import detector_rows, rules
from ..submission.guess import KEYWORDS
from . import super_admin_stats
from .deps import flash, render, require_roles, verify_csrf

router = APIRouter(prefix="/super-admin")
SuperAdmin = Annotated[User, Depends(require_roles(Role.SUPER_ADMIN))]
Db = Annotated[Session, Depends(get_db)]
Page = Annotated[int, Query(ge=1, le=1000000)]
Search = Annotated[str, Query(max_length=100)]
CASE_STATUSES = {"pending": "검토 대기", "applied": "문구 반영", "reviewed": "검토 완료", "rejected": "반려"}
MATCH_LABELS = {"any": "하나 이상 일치", "all": "필수 조건의 대체 표현", "must": "내용 조건의 대체 표현", "reject": "제외 조건"}


def _page(request: Request, name: str, db: Session, user: User, **ctx):
    response = render(request, f"super_admin/{name}.html", db, user, mvp_layout=True, **ctx)
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.get("")
def overview(request: Request, user: SuperAdmin, db: Db, q: Search = "", page: Page = 1):
    return _page(request, "overview", db, user, stats=super_admin_stats.overview(db, q.strip(), page),
                 page=page, q=q, verdict_labels=VERDICTS)


@router.get("/cases")
def cases(request: Request, user: SuperAdmin, db: Db, q: Search = "", page: Page = 1,
          status: Literal["pending", "applied", "reviewed", "rejected", "all"] = "pending"):
    query = select(QCRuleFeedback)
    if status != "all":
        query = query.where(QCRuleFeedback.status == status)
    if q.strip():
        term = q.strip()
        query = query.where(QCRuleFeedback.rule_code.icontains(term, autoescape=True)
                            | QCRuleFeedback.title.icontains(term, autoescape=True))
    size = super_admin_stats.PAGE_SIZE
    rows = db.scalars(query.order_by(QCRuleFeedback.id.desc()).offset((page - 1) * size).limit(size + 1)).all()
    return _page(request, "cases", db, user, cases=rows[:size], has_next=len(rows) > size,
                 page=page, q=q, status=status, statuses=CASE_STATUSES)


def _case(db: Session, case_id: int) -> QCRuleFeedback:
    case = db.get(QCRuleFeedback, case_id)
    if case is None:
        raise HTTPException(404, "사례를 찾을 수 없습니다.")
    return case


def _target_values(case: QCRuleFeedback) -> dict[str, tuple[str, str]]:
    criterion = required_criteria().get(case.rule_code)
    if criterion is None:
        return {}
    return {json.dumps(target, ensure_ascii=False): target for target in keyword_targets(criterion)}


@router.get("/cases/{case_id}")
def case_details(request: Request, case_id: int, user: SuperAdmin, db: Db):
    case = _case(db, case_id)
    keyword = db.scalar(select(QCRuleKeyword).where(QCRuleKeyword.case_id == case.id))
    return _page(request, "case", db, user, case=case, targets=_target_values(case),
                 statuses=CASE_STATUSES, match_labels=MATCH_LABELS, keyword=keyword)


@router.post("/cases/{case_id}", dependencies=[Depends(verify_csrf)])
def decide(request: Request, case_id: int, user: SuperAdmin, db: Db,
           decision: Annotated[Literal["applied", "reviewed", "rejected"], Form()],
           note: Annotated[str, Form(max_length=1000)], keyword: Annotated[str, Form(max_length=80)] = "",
           condition: Annotated[str, Form(max_length=1000)] = ""):
    case = _case(db, case_id)
    target = _target_values(case).get(condition, ("", ""))
    try:
        review_case(db, user, case, decision=decision, note=note, keyword=keyword, target=target)
    except FeedbackConflict as error:
        db.rollback()
        flash(request, str(error), "error")
    else:
        db.commit()
        flash(request, "검토 결과를 저장했습니다.", "success")
    return RedirectResponse(f"/super-admin/cases/{case_id}", status_code=303)


@router.get("/rules")
def rule_catalog(request: Request, user: SuperAdmin, db: Db, q: Search = ""):
    approved = db.scalars(select(QCRuleKeyword).order_by(QCRuleKeyword.rule_code, QCRuleKeyword.id.desc())).all()
    catalog = required_criteria()
    needle = q.strip().casefold()
    matching = {row.rule_code for row in approved if needle in row.keyword.casefold()}
    entries = [row for row in catalog.values() if row["code"] in matching or needle in str(row).casefold()]
    entry_codes = {entry["code"] for entry in entries}
    aliases = [row for row in approved if not needle or needle in row.rule_code.casefold() or row.rule_code in matching
               or row.rule_code in entry_codes]
    return _page(request, "rules", db, user, entries=entries, aliases=aliases, q=q,
                 match_labels=MATCH_LABELS, detectors=detector_rows(True), filename_keywords=KEYWORDS,
                 body_keywords=classification_keywords(), criteria_data=yaml.safe_dump(criteria(), allow_unicode=True, sort_keys=False),
                 registry_version=rules()["version"], criteria_version=criteria().get("version", ""))


@router.post("/rules/keywords/{keyword_id}", dependencies=[Depends(verify_csrf)])
def toggle(request: Request, keyword_id: int, user: SuperAdmin, db: Db,
           active: Annotated[Literal["yes", "no"], Form()]):
    keyword = db.get(QCRuleKeyword, keyword_id)
    if keyword is None:
        raise HTTPException(404, "규칙 문구를 찾을 수 없습니다.")
    if active == "yes":
        criterion = required_criteria().get(keyword.rule_code)
        if criterion is None or (keyword.kind, keyword.pattern) not in keyword_targets(criterion):
            raise HTTPException(409, "현재 기준에서 적용할 조건을 찾을 수 없습니다.")
    set_keyword_active(db, user, keyword.id, active=active == "yes")
    db.commit()
    flash(request, "규칙 문구의 적용 상태를 저장했습니다.", "success")
    return RedirectResponse("/super-admin/rules#approved-keywords", status_code=303)
