"""처음 시작하기·전체 가이드, 샘플 데이터 자료실, 전체 파이프라인 테스트."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..demo import samples as smp
from ..demo.models import DemoRun
from ..help.guides import GUIDES, START_STEPS
from ..models import Role, User
from .deps import client_ip, current_user, flash, render, require_roles, verify_csrf

router = APIRouter()
researcher = require_roles(Role.RESEARCHER)


@router.get("/help")
def help_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    guides = [g for g in GUIDES if not g.admin_only or user.is_admin]
    return render(request, "help/index.html", db, user, guides=guides, start=START_STEPS)


@router.get("/samples")
def samples_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    groups = [(name, [s for s in items if user.is_admin or not s.group.endswith("(관리자)")])
              for name, items in smp.groups()]
    return render(request, "demo/samples.html", db, user, groups=[g for g in groups if g[1]])


@router.get("/samples/{key}")
def sample_file(key: str, user: User = Depends(current_user)):
    s = smp.BY_KEY.get(key)
    if s is None:
        raise HTTPException(404)
    return Response(s.build(), media_type=s.media_type,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(s.filename)}"})


@router.get("/demo")
def demo_page(request: Request, user: User = Depends(researcher), db: Session = Depends(get_db)):
    runs = db.scalars(select(DemoRun).where(DemoRun.created_by == user.id).order_by(DemoRun.id.desc()).limit(10)).all()
    return render(request, "demo/index.html", db, user, runs=runs, ai_on=bool(get_settings().ai_enabled))


@router.post("/demo/run", dependencies=[Depends(verify_csrf)])
async def demo_run(request: Request, user: User = Depends(researcher), db: Session = Depends(get_db)):
    from ..demo.full import run_full_pipeline

    form = await request.form()
    d = run_full_pipeline(db, user, use_ai=form.get("use_ai") == "on" and bool(get_settings().ai_enabled),
                          ip=client_ip(request))
    s = d.summary
    flash(request, f"전체 파이프라인 테스트 완료 - 성공 {s['ok']} · 경고 {s['warn']} · 실패 {s['failed']} · 건너뜀 {s['skipped']}",
          "success" if d.status == "ok" else "info")
    return RedirectResponse(f"/demo/{d.id}", status_code=303)


@router.get("/demo/{did}")
def demo_result(request: Request, did: int, user: User = Depends(researcher), db: Session = Depends(get_db)):
    d = db.get(DemoRun, did)
    if d is None or (d.created_by != user.id and not user.is_admin):
        raise HTTPException(404)
    return render(request, "demo/run.html", db, user, d=d)
