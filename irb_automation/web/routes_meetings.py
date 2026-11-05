"""위원회 회의 화면."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import esign
from ..database import get_db
from ..models import DecisionType, Meeting, MeetingItem, Role, User
from ..workflow import meetings as mt
from ..workflow import service as wf
from .deps import client_ip, flash, render, require_roles, verify_csrf
from .routes_protocols import decision_payload

router = APIRouter()
staff_only = require_roles(Role.COORDINATOR, Role.CHAIR)
committee = require_roles(Role.COORDINATOR, Role.CHAIR, Role.REVIEWER)


def _meeting(db: Session, mid: int) -> Meeting:
    m = db.get(Meeting, mid)
    if m is None:
        raise HTTPException(404)
    return m


@router.get("/meetings")
def meetings(request: Request, user: User = Depends(committee), db: Session = Depends(get_db)):
    items = db.scalars(select(Meeting).order_by(Meeting.scheduled_at.desc())).all()
    return render(request, "meetings/list.html", db, user, items=items, composition=mt.composition_check(db))


@router.post("/meetings", dependencies=[Depends(verify_csrf)])
async def meeting_create(request: Request, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    form = await request.form()
    try:
        at = datetime.fromisoformat(str(form.get("scheduled_at", "")))
    except ValueError:
        flash(request, "회의 일시를 입력하십시오.", "error")
        return RedirectResponse("/meetings", status_code=303)
    m = mt.create_meeting(db, user, at, str(form.get("location", "")), client_ip(request))
    added = mt.build_agenda(db, user, m, client_ip(request))
    db.commit()
    flash(request, f"{m.label} 회의를 생성하고 안건을 자동 구성했습니다(심의 {added['review']}건, 보고 {added['report']}건).", "success")
    return RedirectResponse(f"/meetings/{m.id}", status_code=303)


@router.get("/meetings/composition")
def composition(request: Request, user: User = Depends(committee), db: Session = Depends(get_db)):
    return render(request, "meetings/composition.html", db, user, c=mt.composition_check(db))


@router.get("/meetings/{mid}")
def meeting_detail(request: Request, mid: int, user: User = Depends(committee), db: Session = Depends(get_db)):
    m = _meeting(db, mid)
    members = mt.committee_members(db)
    quorum = {i.id: mt.quorum_status(db, m, i) for i in m.items}
    return render(request, "meetings/detail.html", db, user, m=m, members=members, quorum=quorum,
                  base_quorum=mt.quorum_status(db, m), vote_keys=mt.VOTE_KEYS, vote_labels=mt.VOTE_LABELS)


@router.post("/meetings/{mid}/agenda", dependencies=[Depends(verify_csrf)])
def meeting_agenda(request: Request, mid: int, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    m = _meeting(db, mid)
    try:
        added = mt.build_agenda(db, user, m, client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/meetings/{mid}", status_code=303)
    db.commit()
    flash(request, f"안건 추가: 심의 {added['review']}건, 보고 {added['report']}건", "success")
    return RedirectResponse(f"/meetings/{mid}", status_code=303)


@router.post("/meetings/{mid}/attendance", dependencies=[Depends(verify_csrf)])
async def meeting_attendance(request: Request, mid: int, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    m = _meeting(db, mid)
    form = await request.form()
    ids = {int(v) for v in form.getlist("present") if str(v).isdigit()}
    mt.set_attendance(db, user, m, ids, client_ip(request))
    db.commit()
    q = mt.quorum_status(db, m)
    flash(request, "출석을 저장했습니다. " + ("의사정족수 충족" if q["ok"] else "정족수 미충족: " + "; ".join(q["reasons"])),
          "success" if q["ok"] else "error")
    return RedirectResponse(f"/meetings/{mid}", status_code=303)


@router.post("/meetings/items/{iid}/vote", dependencies=[Depends(verify_csrf)])
async def item_vote(request: Request, iid: int, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    item = db.get(MeetingItem, iid)
    if item is None:
        raise HTTPException(404)
    form = await request.form()
    votes = {k: int(form.get(f"v_{k}") or 0) for k in mt.VOTE_KEYS}
    recused = [int(v) for v in form.getlist("recused") if str(v).isdigit()]
    try:
        d = mt.record_item_decision(db, user, item, votes, recused, str(form.get("conditions", "")),
                                    str(form.get("comments", "")), str(form.get("discussion", "")), client_ip(request))
        esign.record_signature(db, user, "심의결과 확정(위원회 의결)", "decision", d.id, decision_payload(d), client_ip(request))
    except (wf.WorkflowError, ValueError) as e:
        db.rollback()
        flash(request, str(e), "error")
        return RedirectResponse(f"/meetings/{item.meeting_id}#item-{iid}", status_code=303)
    db.commit()
    flash(request, f"의결: {d.decision.label} (통지서 {d.letter_no})", "success")
    return RedirectResponse(f"/meetings/{item.meeting_id}#item-{iid}", status_code=303)


@router.post("/meetings/items/{iid}/remove", dependencies=[Depends(verify_csrf)])
def item_remove(request: Request, iid: int, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    item = db.get(MeetingItem, iid)
    if item is None:
        raise HTTPException(404)
    mid = item.meeting_id
    try:
        mt.remove_item(db, user, item, client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/meetings/{mid}", status_code=303)
    db.commit()
    return RedirectResponse(f"/meetings/{mid}", status_code=303)


@router.post("/meetings/{mid}/close", dependencies=[Depends(verify_csrf)])
async def meeting_close(request: Request, mid: int, user: User = Depends(staff_only), db: Session = Depends(get_db)):
    m = _meeting(db, mid)
    form = await request.form()
    try:
        mt.close_meeting(db, user, m, str(form.get("notes", "")), client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/meetings/{mid}", status_code=303)
    db.commit()
    flash(request, "회의를 종료했습니다. 회의록을 출력하십시오.", "success")
    return RedirectResponse(f"/meetings/{mid}/minutes", status_code=303)


@router.get("/meetings/{mid}/minutes")
def minutes(request: Request, mid: int, user: User = Depends(committee), db: Session = Depends(get_db)):
    m = _meeting(db, mid)
    members = mt.committee_members(db)
    return render(request, "print/minutes.html", db, user, m=m, members=members,
                  base_quorum=mt.quorum_status(db, m), vote_labels=mt.VOTE_LABELS, DecisionType=DecisionType)
