"""대상자 수 사전 조회, 통합 검색."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import feasibility
from ..database import get_db
from ..models import DeidJob, FeasibilityQuery, Protocol, User
from ..timeutil import parse_date
from ..workflow import service as wf
from .deps import client_ip, current_user, flash, render, verify_csrf

router = APIRouter()


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
    protocols, jobs = [], []
    if len(q) >= 2:
        like = f"%{q}%"
        protocols = [p for p in db.scalars(
            select(Protocol).join(User, Protocol.pi_id == User.id).where(or_(
                Protocol.title.ilike(like), Protocol.protocol_no.ilike(like), User.full_name.ilike(like),
                Protocol.objectives.ilike(like), Protocol.department.ilike(like)))
        ) if wf.can_view(db, user, p)]
        jobs = [j for j in db.scalars(select(DeidJob).where(DeidJob.original_filename.ilike(like)))
                if user.is_admin or j.created_by == user.id]
    return render(request, "search.html", db, user, q=q, protocols=protocols, jobs=jobs)

