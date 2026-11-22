"""Super-admin catalog and editing for document rule patterns."""
from __future__ import annotations

import json
import unicodedata
from collections import defaultdict
from typing import Annotated, Literal
from urllib.parse import urlencode

import re2
import yaml
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import audit
from ..database import get_db
from ..engine.ruleset import get_ruleset
from ..models import Role, User
from ..qc.checks import criteria
from ..qc.feedback import keyword_targets, required_criteria
from ..qc.models import QCRuleKeyword
from ..qc.pattern_history import StateChange, change_active, record_registration
from ..qc.pattern_models import DocumentRuleChange, DocumentRulePattern
from ..qc.pattern_store import Target, new_pattern, use_database_settings
from ..qc.patterns import Format, Scope, compile_expression
from ..qc.rules import detector_rows, rules
from .deps import flash, render, require_roles, verify_csrf

router = APIRouter(prefix="/rules")
SuperAdmin = Annotated[User, Depends(require_roles(Role.SUPER_ADMIN))]
Db = Annotated[Session, Depends(get_db)]
Search = Annotated[str, Query(max_length=100)]
Document = Annotated[str, Query(max_length=40)]
HistoryPage = Annotated[int, Query(ge=1, le=100000)]
HISTORY_PAGE_SIZE = 20

MATCH_LABELS = {
    "any": "하나 이상 일치", "all": "필수 조건의 대체 표현", "must": "내용 조건의 대체 표현",
    "reject": "제외 조건", "equivalent_title": "대체 섹션 제목", "equivalent_body": "대체 섹션 본문 조건",
    "deny": "철회 권리 모순",
}
CLASSIFICATION_LABELS = {"filename": "파일명 분류", "body": "본문 분류", "structure": "계획서 구성 판정"}


def _back(doc_type: str = "", q: str = "", anchor: str = "") -> RedirectResponse:
    query = urlencode({key: value for key, value in (("doc_type", doc_type), ("q", q)) if value})
    return RedirectResponse("/super-admin/rules" + (f"?{query}" if query else "") + (f"#{anchor}" if anchor else ""),
                            status_code=303)


@router.get("")
def rule_catalog(request: Request, user: SuperAdmin, db: Db, q: Search = "", doc_type: Document = "",
                 history_page: HistoryPage = 1):
    types = dict(get_ruleset().options("document_types"))
    if doc_type and doc_type not in types:
        raise HTTPException(422, "문서 종류를 확인해 주세요.")
    with use_database_settings(db):
        catalog = required_criteria()
        config = criteria()
        registry_version = rules()["version"]
        detectors = detector_rows(True) if not doc_type and not q.strip() else []
    query = select(DocumentRulePattern).order_by(DocumentRulePattern.id)
    if doc_type:
        query = query.where(DocumentRulePattern.doc_type == doc_type)
    stored = db.scalars(query).all()
    required = defaultdict(list)
    classification = defaultdict(list)
    for row in stored:
        if row.scope == "required":
            required[row.rule_code].append(row)
        else:
            classification[row.doc_type, row.scope].append(row)

    approved = db.scalars(select(QCRuleKeyword).order_by(QCRuleKeyword.rule_code, QCRuleKeyword.id.desc())).all()
    needle = q.strip().casefold()
    matching = {row.rule_code for row in approved if needle in row.keyword.casefold()}
    entries = [entry for code, entry in catalog.items() if (not doc_type or entry["doc_type"] == doc_type) and (
        code in matching or needle in f"{code} {entry['label']} {types.get(entry['doc_type'], '')}".casefold()
        or any(needle in row.expression.casefold() for row in required[code]))]
    codes = {entry["code"] for entry in entries}
    aliases = [row for row in approved if row.rule_code in codes or (row.rule_code not in catalog and not doc_type
               and (needle in row.rule_code.casefold() or needle in row.keyword.casefold()))]
    classes = [
        (key, label, [(scope, name, classification[key, scope]) for scope, name in CLASSIFICATION_LABELS.items()
                      if scope != "structure" or key == "protocol"])
        for key, label in types.items() if key != "other" and (not doc_type or key == doc_type)
        and (not needle or needle in f"{key} {label}".casefold() or any(
            needle in row.expression.casefold() for scope in CLASSIFICATION_LABELS for row in classification[key, scope]))
    ]
    selected = request.query_params.get("open", "")
    history = _changes(db, doc_type, q, history_page)
    response = render(
        request, "super_admin/rules.html", db, user, mvp_layout=True, entries=entries, required=required,
        aliases=aliases, classes=classes, q=q, doc_type=doc_type, doc_types=types, selected=selected,
        form_scope=request.query_params.get("form", ""), classification_labels=CLASSIFICATION_LABELS,
        match_labels=MATCH_LABELS, detectors=detectors, registry_version=registry_version,
        history=history[:HISTORY_PAGE_SIZE], history_page=history_page, history_has_next=len(history) > HISTORY_PAGE_SIZE,
        criteria_version=config.get("version", ""),
        criteria_data=yaml.safe_dump({key: value for key, value in config.items() if key != "required"},
                                     allow_unicode=True, sort_keys=False) if detectors else "",
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _changes(db: Session, doc_type: str, q: str, page: int):
    expression = func.coalesce(DocumentRulePattern.expression, QCRuleKeyword.keyword)
    rule_code = func.coalesce(DocumentRulePattern.rule_code, QCRuleKeyword.rule_code)
    condition = func.coalesce(DocumentRulePattern.condition, QCRuleKeyword.kind)
    target = func.coalesce(DocumentRulePattern.target, QCRuleKeyword.pattern)
    query = (select(DocumentRuleChange, User.username, expression, rule_code, DocumentRulePattern.scope,
                    DocumentRulePattern.format, condition, target)
             .select_from(DocumentRuleChange)
             .join(User, User.id == DocumentRuleChange.actor_id)
             .outerjoin(DocumentRulePattern, DocumentRulePattern.id == DocumentRuleChange.pattern_id)
             .outerjoin(QCRuleKeyword, QCRuleKeyword.id == DocumentRuleChange.alias_id))
    if doc_type:
        query = query.where(DocumentRuleChange.doc_type == doc_type)
    if q.strip():
        query = query.where(expression.icontains(q.strip(), autoescape=True) | rule_code.icontains(q.strip(), autoescape=True))
    return db.execute(query.order_by(DocumentRuleChange.id.desc()).offset((page - 1) * HISTORY_PAGE_SIZE)
                      .limit(HISTORY_PAGE_SIZE + 1)).all()


def _target(db: Session, scope: Scope, doc_type: str, rule_code: str, condition: str) -> Target:
    if doc_type not in get_ruleset().catalog("document_types") or doc_type == "other":
        raise HTTPException(422, "문서 종류를 확인해 주세요.")
    if scope != "required":
        if scope == "structure" and doc_type != "protocol":
            raise HTTPException(422, "연구계획서에만 적용할 수 있습니다.")
        return Target(scope, doc_type)
    entry = required_criteria(db).get(rule_code)
    if entry is None or entry["doc_type"] != doc_type:
        raise HTTPException(422, "문서 종류와 필수항목을 확인해 주세요.")
    try:
        posted = json.loads(condition)
    except json.JSONDecodeError as error:
        raise HTTPException(422, "매칭 조건을 확인해 주세요.") from error
    for kind, anchor in entry["groups"]:
        if posted == [kind, anchor]:
            return Target(scope, doc_type, rule_code, kind, anchor)
    raise HTTPException(422, "매칭 조건을 확인해 주세요.")


def _pattern_back(doc_type: str, anchor: str, *, q: str = "") -> RedirectResponse:
    query = urlencode({"doc_type": doc_type, "open": anchor, **({"q": q} if q else {})})
    return RedirectResponse(f"/super-admin/rules?{query}#{anchor}", status_code=303)


@router.post("/patterns", dependencies=[Depends(verify_csrf)])
def add_pattern(request: Request, user: SuperAdmin, db: Db, scope: Annotated[Scope, Form()],
                doc_type: Annotated[str, Form(max_length=40)], format: Annotated[Format, Form()],
                expression: Annotated[str, Form(max_length=240)], rule_code: Annotated[str, Form(max_length=100)] = "",
                condition: Annotated[str, Form(max_length=1000)] = "",
                weight: Annotated[float, Form(gt=0, le=20)] = 5):
    target = _target(db, scope, doc_type, rule_code, condition)
    anchor = target.rule_code or f"class-{doc_type}"
    back = _pattern_back(doc_type, anchor)
    invalid = RedirectResponse("/super-admin/rules?" + urlencode({"doc_type": doc_type, "open": anchor, "form": scope}),
                               status_code=303)
    value = expression.strip()
    if format == "literal":
        value = unicodedata.normalize("NFC", value)
    if not value:
        flash(request, "등록할 문구를 입력해 주세요.", "error")
        return invalid
    if format == "regex":
        try:
            matches_empty = compile_expression(value).search("") is not None
        except re2.error:
            flash(request, "정규표현식 문법을 확인해 주세요. 전후방 탐색과 역참조는 지원하지 않습니다.", "error")
            return invalid
        if matches_empty:
            flash(request, "빈 문자열과 일치하는 정규표현식은 등록할 수 없습니다.", "error")
            return invalid
    row = new_pattern(target, value, format, weight=weight if scope == "body" else 1.0)
    row.created_by = user.id
    if scope != "required":
        priority = db.scalar(select(DocumentRulePattern.priority).where(
            DocumentRulePattern.scope == scope, DocumentRulePattern.doc_type == doc_type,
        ).order_by(DocumentRulePattern.priority).limit(1))
        row.priority = priority if priority is not None else 10000
    try:
        db.add(row)
        db.flush()
    except IntegrityError:
        db.rollback()
        flash(request, "같은 문구가 이미 등록되어 있습니다. 기존 문구의 적용 상태를 확인해 주세요.", "error")
        return invalid
    audit.record(db, user, "document_rule_pattern_add", "document_rule_pattern", row.id,
                 {"scope": scope, "doc_type": doc_type, "rule_code": rule_code, "format": format})
    record_registration(db, user, row, doc_type)
    db.commit()
    flash(request, "문구를 등록했습니다. 다음 실행부터 적용됩니다.", "success")
    return back


@router.post("/patterns/{pattern_id}", dependencies=[Depends(verify_csrf)])
def toggle_pattern(request: Request, pattern_id: int, user: SuperAdmin, db: Db,
                   active: Annotated[Literal["yes", "no"], Form()], q: Annotated[str, Form(max_length=100)] = ""):
    row = db.get(DocumentRulePattern, pattern_id)
    if row is None:
        raise HTTPException(404, "규칙 문구를 찾을 수 없습니다.")
    if change_active(db, user, row, StateChange(row.doc_type, active == "yes")):
        db.commit()
    flash(request, "문구의 적용 상태를 저장했습니다.", "success")
    return _pattern_back(row.doc_type, row.rule_code or f"class-{row.doc_type}", q=q)


@router.post("/keywords/{keyword_id}", dependencies=[Depends(verify_csrf)])
def toggle_alias(request: Request, keyword_id: int, user: SuperAdmin, db: Db,
                 active: Annotated[Literal["yes", "no"], Form()],
                 doc_type: Annotated[str, Form(max_length=40)] = "", q: Annotated[str, Form(max_length=100)] = ""):
    keyword = db.get(QCRuleKeyword, keyword_id)
    if keyword is None:
        raise HTTPException(404, "규칙 문구를 찾을 수 없습니다.")
    criterion = required_criteria(db).get(keyword.rule_code)
    if active == "yes" and (criterion is None or (keyword.kind, keyword.pattern) not in keyword_targets(criterion)):
        raise HTTPException(409, "현재 기준에서 적용할 조건을 찾을 수 없습니다.")
    change_active(db, user, keyword, StateChange(criterion["doc_type"] if criterion else "", active == "yes"))
    db.commit()
    flash(request, "규칙 문구의 적용 상태를 저장했습니다.", "success")
    return _back(doc_type, q, "approved-keywords")


@router.post("/history/{change_id}/restore", dependencies=[Depends(verify_csrf)])
def restore_pattern(request: Request, change_id: int, user: SuperAdmin, db: Db,
                    doc_type: Annotated[str, Form(max_length=40)] = "", q: Annotated[str, Form(max_length=100)] = ""):
    change = db.get(DocumentRuleChange, change_id)
    if change is None:
        raise HTTPException(404, "변경 이력을 찾을 수 없습니다.")
    active = bool(change.before_active)
    if change.pattern_id is not None:
        row = db.get(DocumentRulePattern, change.pattern_id)
    else:
        row = db.get(QCRuleKeyword, change.alias_id)
        if row is not None and active:
            criterion = required_criteria(db).get(row.rule_code)
            if criterion is None or (row.kind, row.pattern) not in keyword_targets(criterion):
                raise HTTPException(409, "현재 기준에서 적용할 조건을 찾을 수 없습니다.")
    if row is None:
        raise HTTPException(404, "복원할 문구를 찾을 수 없습니다.")
    if change_active(db, user, row, StateChange(change.doc_type, active, change.id)):
        db.commit()
        flash(request, "이전 적용 상태로 복원했습니다.", "success")
    else:
        flash(request, "이미 해당 상태로 적용되어 있습니다.", "success")
    return _back(doc_type, q, "rule-history")
