"""성과지표, 연간 운영실적, 내보내기, 대상자 수 사전 조회, 통합 검색."""
from __future__ import annotations

import csv
import io
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import audit, feasibility
from ..database import get_db
from ..metrics import annual_report, kpi
from ..models import Agreement, DeidJob, FeasibilityQuery, Protocol, Role, User
from ..timeutil import parse_date, today
from ..workflow import service as wf
from .deps import client_ip, current_user, flash, render, require_roles, verify_csrf

router = APIRouter()
staff_only = require_roles(Role.COORDINATOR, Role.CHAIR)


@router.get("/reports/kpi")
def kpi_page(request: Request, start: str = "", end: str = "", user: User = Depends(staff_only), db: Session = Depends(get_db)):
    e = parse_date(end) or today()
    s = parse_date(start) or (e - timedelta(days=365))
    return render(request, "reports/kpi.html", db, user, k=kpi(db, s, e), start=s, end=e)


@router.get("/reports/annual")
def annual_page(request: Request, year: int | None = None, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    y = year or today().year
    return render(request, "print/annual.html", db, user, r=annual_report(db, y))


@router.get("/reports/protocols.csv")
def protocols_csv(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["과제번호", "제목", "연구책임자", "부서", "상태", "심의유형", "위험도", "위험점수", "승인일", "만료일",
                "연구유형", "처리수준", "대상자수", "외부반출", "다기관", "최초접수일"])
    for p in db.scalars(select(Protocol).where(Protocol.protocol_no.is_not(None)).order_by(Protocol.protocol_no)):
        first = p.submissions[0].submitted_at.date() if p.submissions else ""
        w.writerow([p.protocol_no, p.title, p.pi.full_name, p.department, p.status.label,
                    p.review_type.label if p.review_type else "", p.risk_level or "", p.risk_score or "",
                    p.approved_at or "", p.expires_at or "", p.study_type, p.deid_level, p.expected_subjects,
                    "Y" if p.external_transfer else "N", "Y" if p.multicenter else "N", first])
    audit.record(db, user, "export", "protocols", None, {"format": "csv"}, client_ip(request))
    db.commit()
    return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=irb_protocols.csv"})


# ------------------------------------------------------------------ 사전 대상자 수 조회
@router.get("/feasibility")
def feasibility_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    history = db.scalars(
        select(FeasibilityQuery).where(FeasibilityQuery.user_id == user.id).order_by(FeasibilityQuery.id.desc()).limit(20)
    ).all()
    return render(request, "reports/feasibility.html", db, user, history=history, result=None, form={},
                  source=feasibility.get_connector().name)


@router.post("/feasibility", dependencies=[Depends(verify_csrf)])
async def feasibility_run(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    form = await request.form()

    def _int(name: str) -> int | None:
        v = str(form.get(name, "")).strip()
        return int(v) if v.isdigit() else None

    criteria = feasibility.Criteria(
        dx_codes=[c.strip().upper() for c in str(form.get("dx_codes", "")).replace(",", " ").split() if c.strip()],
        sex=str(form.get("sex", "")),
        age_min=_int("age_min"),
        age_max=_int("age_max"),
        date_from=parse_date(str(form.get("date_from", ""))),
        date_to=parse_date(str(form.get("date_to", ""))),
    )
    result = None
    try:
        result = feasibility.run_query(db, user, criteria, ip=client_ip(request))
        db.commit()
    except feasibility.FeasibilityError as e:
        flash(request, str(e), "error")
    history = db.scalars(
        select(FeasibilityQuery).where(FeasibilityQuery.user_id == user.id).order_by(FeasibilityQuery.id.desc()).limit(20)
    ).all()
    return render(request, "reports/feasibility.html", db, user, history=history, result=result, form=dict(form),
                  source=feasibility.get_connector().name)


# ------------------------------------------------------------------ 통합 검색
@router.get("/search")
def search(request: Request, q: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    q = q.strip()
    protocols, jobs, agreements = [], [], []
    if len(q) >= 2:
        like = f"%{q}%"
        protocols = [p for p in db.scalars(
            select(Protocol).join(User, Protocol.pi_id == User.id).where(or_(
                Protocol.title.ilike(like), Protocol.protocol_no.ilike(like), User.full_name.ilike(like),
                Protocol.objectives.ilike(like), Protocol.department.ilike(like)))
        ) if wf.can_view(db, user, p)]
        if user.role in (Role.DATA_STEWARD, Role.ADMIN):
            jobs = db.scalars(select(DeidJob).where(DeidJob.original_filename.ilike(like))).all()
        if user.is_irb_staff or user.role == Role.DATA_STEWARD:
            agreements = db.scalars(select(Agreement).where(or_(Agreement.counterparty.ilike(like), Agreement.title.ilike(like)))).all()
    return render(request, "search.html", db, user, q=q, protocols=protocols, jobs=jobs, agreements=agreements)

