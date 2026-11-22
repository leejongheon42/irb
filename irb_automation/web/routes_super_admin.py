"""Highest administrator access to service totals, rule criteria, and review decisions."""
from __future__ import annotations

import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Role, User
from ..qc.category_scores import VERDICTS
from ..qc.feedback import FeedbackConflict, keyword_targets, required_criteria, review_case
from ..qc.models import QCRuleFeedback, QCRuleKeyword
from . import super_admin_stats
from .deps import flash, render, require_roles, verify_csrf
from .routes_super_admin_rules import MATCH_LABELS
from .routes_super_admin_rules import router as rules_router

router = APIRouter(prefix="/super-admin")
SuperAdmin = Annotated[User, Depends(require_roles(Role.SUPER_ADMIN))]
Db = Annotated[Session, Depends(get_db)]
Page = Annotated[int, Query(ge=1, le=1000000)]
Search = Annotated[str, Query(max_length=100)]
CASE_STATUSES = {"pending": "검토 대기", "applied": "문구 반영", "reviewed": "검토 완료", "rejected": "반려"}
router.include_router(rules_router)


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


def _target_values(db: Session, case: QCRuleFeedback) -> dict[str, tuple[str, str]]:
    criterion = required_criteria(db).get(case.rule_code)
    if criterion is None:
        return {}
    return {json.dumps(target, ensure_ascii=False): target for target in keyword_targets(criterion)}


@router.get("/cases/{case_id}")
def case_details(request: Request, case_id: int, user: SuperAdmin, db: Db):
    case = _case(db, case_id)
    keyword = db.scalar(select(QCRuleKeyword).where(QCRuleKeyword.case_id == case.id))
    return _page(request, "case", db, user, case=case, targets=_target_values(db, case),
                 statuses=CASE_STATUSES, match_labels=MATCH_LABELS, keyword=keyword)


@router.post("/cases/{case_id}", dependencies=[Depends(verify_csrf)])
def decide(request: Request, case_id: int, user: SuperAdmin, db: Db,
           decision: Annotated[Literal["applied", "reviewed", "rejected"], Form()],
           note: Annotated[str, Form(max_length=1000)], keyword: Annotated[str, Form(max_length=80)] = "",
           condition: Annotated[str, Form(max_length=1000)] = ""):
    case = _case(db, case_id)
    target = _target_values(db, case).get(condition, ("", ""))
    try:
        review_case(db, user, case, decision=decision, note=note, keyword=keyword, target=target)
    except FeedbackConflict as error:
        db.rollback()
        flash(request, str(error), "error")
    else:
        db.commit()
        flash(request, "검토 결과를 저장했습니다.", "success")
    return RedirectResponse(f"/super-admin/cases/{case_id}", status_code=303)
