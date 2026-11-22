"""제출 서류 패키지(자동 완성)·서류 품질 점검·보완 대응 화면."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.datastructures import FormData

from .. import audit
from ..config import get_settings
from ..database import get_db
from ..models import Protocol, Role, User
from ..package import revision as rv
from ..package import service as pkg
from ..package.drafting import FIELDS as DRAFT_FIELDS
from ..qc import service as qcs
from ..qc.checks import criteria
from ..qc.models import QCRun
from ..qc.profile import Answers
from ..qc.review import CHECK_OPTIONS, ReviewError, refresh, resolve_check, select_reference, transition
from ..qc.rules import detector_rows
from ..qc.samples import SAMPLES
from ..timeutil import now
from ..workflow import service as wf
from .deps import client_ip, current_user, flash, render, require_roles, verify_csrf

router = APIRouter()
researcher = require_roles(Role.RESEARCHER)


def _qc_profile(form: FormData) -> Answers:
    answers: Answers = {}
    committee = str(form.get("committee", ""))
    if committee in ("public_irb", "institution"):
        answers["committee"] = committee
    for key in ("multicenter", "student_pi", "compare_only"):
        if form.get(key) in ("yes", "no"):
            answers[key] = form.get(key) == "yes"
    vulnerable = [str(v) for v in form.getlist("vulnerable") if v in ("children", "proxy")]
    if vulnerable or "none" in form.getlist("vulnerable"):
        answers["vulnerable"] = vulnerable
    study = str(form.get("study_type", ""))
    application = str(form.get("application", ""))
    sources = [str(v) for v in form.getlist("data_source") if v in ("collect", "existing_internal", "external", "public", "bank")]
    consent = [str(v) for v in form.getlist("consent") if v in ("obtain", "waiver_request", "existing_consent", "not_applicable")]
    method = [str(v) for v in form.getlist("method") if v in ("survey", "intervention", "records", "specimen", "stem_cell")]
    if study in ("human", "specimen", "both"):
        answers["study_type"] = study
    if application in ("new", "exemption"):
        answers["application"] = application
    if sources:
        answers["data_source"] = sources
    if consent:
        answers["consent"] = consent
    if method:
        answers["method"] = method
    if form.get("drug") in ("yes", "no"):
        answers["drug"] = form.get("drug") == "yes"
    if form.get("visits") in ("yes", "no"):
        answers["visits"] = form.get("visits") == "yes"
    return answers


def _protocol(db: Session, user: User, pid: int) -> Protocol:
    p = db.get(Protocol, pid)
    if p is None or not wf.can_view(db, user, p):
        raise HTTPException(404, "과제를 찾을 수 없습니다.")
    return p


def _ai_on() -> bool:
    return bool(get_settings().ai_enabled)


# ------------------------------------------------------------------ 패키지
@router.get("/protocols/{pid}/package")
def package_page(request: Request, pid: int, kind: str = "", user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    try:
        pl = pkg.plan(db, p, kind or None)
    except ValueError as e:
        raise HTTPException(400, "알 수 없는 제출 구분입니다.") from e
    return render(request, "package/plan.html", db, user, p=p, plan=pl, is_member=wf.is_member(user, p),
                  ai_on=_ai_on(), draft_labels=DRAFT_FIELDS, last=request.session.pop("autocomplete", None))


@router.post("/protocols/{pid}/package/autocomplete", dependencies=[Depends(verify_csrf)])
async def package_autocomplete(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    form = await request.form()
    try:
        out = pkg.autocomplete(db, user, p, use_ai=form.get("use_ai") == "on", draft_fields=form.get("draft") != "off",
                               regenerate=form.get("regenerate") == "on", kind=str(form.get("kind") or "") or None,
                               ip=client_ip(request))
    except wf.WorkflowError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/protocols/{pid}/package", status_code=303)
    db.commit()
    request.session["autocomplete"] = {
        "drafted": {k: DRAFT_FIELDS.get(k, k) + (" (AI)" if v == "ai" else " (표준 문안)") for k, v in out["drafted"].items()},
        "generated": [f"{g['label']} v{g['version']}" for g in out["generated"]],
        "failed": [f"{f['label']}: {f['error'][:120]}" for f in out["failed"]],
        "messages": out["messages"],
        "qc": out["qc_run"].id if out["qc_run"] else None,
    }
    if out["qc_run"]:
        qr = out["qc_run"]
        flash(request, f"자동 완성 완료 - 서류 {len(out['generated'])}건 작성, 점검 결과 {qr.score}점 {qr.verdict}.",
              "success" if qr.passed else "info")
    return RedirectResponse(f"/protocols/{pid}/package", status_code=303)


@router.get("/protocols/{pid}/package.zip")
def package_zip(pid: int, kind: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    data, name = pkg.build_zip(db, user, p, kind or None)
    db.commit()
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})


# ------------------------------------------------------------------ 서류 점검
@router.get("/qc")
def qc_home(request: Request, user: User = Depends(researcher), db: Session = Depends(get_db)):
    runs = db.scalars(select(QCRun).where(QCRun.created_by == user.id, QCRun.mvp_attempt.is_(None))
                      .order_by(QCRun.id.desc()).limit(30)).all()
    protocols = [p for p in db.scalars(select(Protocol).order_by(Protocol.updated_at.desc())) if wf.is_member(user, p)]
    return render(request, "qc/index.html", db, user, runs=runs, protocols=protocols, ai_on=_ai_on(),
                  doc_types=_doc_type_options(), criteria=criteria(db))


def _doc_type_options() -> list[tuple[str, str]]:
    from ..engine.ruleset import get_ruleset

    return get_ruleset().options("document_types")


@router.post("/qc", dependencies=[Depends(verify_csrf)])
async def qc_upload(request: Request, user: User = Depends(researcher), db: Session = Depends(get_db)):
    form = await request.form()
    files = []
    if form.get("sample") == "1":
        files = [(n, f(), t) for n, t, f in SAMPLES]
    else:
        for i, up in enumerate(form.getlist("files")):
            if hasattr(up, "read") and getattr(up, "filename", ""):
                raw = await up.read()
                dt = str(form.get(f"doc_type_{i}", "") or "")
                files.append((Path(up.filename).name, raw, dt if dt and dt != "auto" else None))
    pid = str(form.get("protocol_id", ""))
    protocol = db.get(Protocol, int(pid)) if pid.isdigit() else None
    if protocol is not None and not wf.can_view(db, user, protocol):
        protocol = None
    if not files:
        flash(request, "점검할 파일을 선택하십시오.", "error")
        return RedirectResponse("/qc", status_code=303)
    try:
        qr = qcs.run_uploads(db, user, files, use_ai=form.get("use_ai") == "on", protocol=protocol,
                             ip=client_ip(request), profile=_qc_profile(form))
    except qcs.QCError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/qc", status_code=303)
    db.commit()
    return RedirectResponse(f"/qc/{qr.id}", status_code=303)


@router.get("/protocols/{pid}/qc")
def protocol_qc_page(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    last = db.scalars(select(QCRun).where(QCRun.protocol_id == p.id).order_by(QCRun.id.desc())).first()
    if last is not None:
        return RedirectResponse(f"/qc/{last.id}", status_code=303)
    return RedirectResponse(f"/protocols/{pid}/package#qc", status_code=303)


@router.post("/protocols/{pid}/qc", dependencies=[Depends(verify_csrf)])
async def protocol_qc_run(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    wf.require_member(user, p)
    form = await request.form()
    ids = [int(x) for x in form.getlist("doc") if str(x).isdigit()]
    try:
        qr = qcs.run_for_protocol(db, user, p, doc_ids=ids or None, use_ai=form.get("use_ai") == "on", ip=client_ip(request))
    except qcs.QCError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/protocols/{pid}/package", status_code=303)
    db.commit()
    return RedirectResponse(f"/qc/{qr.id}", status_code=303)


def _run(db: Session, user: User, rid: int) -> QCRun:
    qr = db.get(QCRun, rid)
    if qr is None or qr.mvp_attempt is not None or not (qr.created_by == user.id or user.is_admin
                          or (qr.protocol is not None and wf.can_view(db, user, qr.protocol))):
        raise HTTPException(404)
    return qr


def _can_edit(user: User, qr: QCRun) -> bool:
    return qr.created_by == user.id or (qr.protocol is not None and wf.is_member(user, qr.protocol))


@router.post("/qc/{rid}/issues/{iid}/{operation}", dependencies=[Depends(verify_csrf)])
async def qc_issue_decision(request: Request, rid: int, iid: int, operation: str,
                            user: User = Depends(current_user), db: Session = Depends(get_db)):
    qr = _run(db, user, rid)
    if not _can_edit(user, qr):
        raise HTTPException(403)
    issue = qr.issue(iid)
    if issue is None or operation not in ("review", "reference", "bundle-answer", "resolve-check"):
        raise HTTPException(404)
    form = await request.form()
    try:
        if operation == "reference":
            index = str(form.get("index", ""))
            if not index.isascii() or not index.isdecimal() or len(index) > 7:
                raise ReviewError("기준 문서를 선택해 주세요.")
            changed = select_reference(issue, int(index))
        elif operation == "resolve-check":
            changed = resolve_check(issue, {"answer": str(form.get("answer", "")),
                                            "note": str(form.get("note", ""))[:1000],
                                            "basis": str(form.get("basis", ""))[:1000]})
            changed["check_answer"].update(by=user.id, at=now().isoformat(timespec="minutes"))
            qr.checks = [dict(c, result=changed["result"], reason=changed["reason"]) if c.get("issue") == iid else c
                         for c in qr.checks or []]
        elif operation == "bundle-answer":
            unit = next((c for c in qr.checks if c.get("issue") == iid and c["rule"] == "E10"
                         and (c["reason"] in ("file_missing", "applicability_unknown")
                              or issue.get("bundle_answer") == "later")), None)
            answer = str(form.get("answer", ""))
            if not unit or issue.get("review") != "candidate" or (
                answer not in ("not_applicable", "later", "out_of_scope")
                and not (answer == "eirb" and unit.get("channel") == "eirb_or_file")
            ):
                raise ReviewError("이 제출 항목에 사용할 수 없는 답변입니다.")
            note = {"eirb": "e-IRB 입력으로 제출", "not_applicable": "제출자가 해당 없음으로 표시",
                    "later": "추후 제출", "out_of_scope": "이번 점검 범위 아님"}[answer]
            qr.profile = {**qr.profile, "bundle_answers": {
                **qr.profile.get("bundle_answers", {}), unit["field"]: answer}}
            result = "needs_check" if answer == "later" else "na"
            qr.checks = [dict(c, result=result, reason=note) if c.get("issue") == iid else c for c in qr.checks]
            changed = dict(issue, review="candidate" if answer == "later" else "resolved", result=result,
                           bundle_answer=answer, review_note=note, reviewed_by=user.id,
                           reviewed_at=now().isoformat(timespec="minutes"))
        else:
            changed = transition(issue, {"action": str(form.get("action", "")),
                                 "severity": str(form.get("severity", "")), "note": str(form.get("note", ""))[:1000]})
            changed.update(reviewed_by=user.id, reviewed_at=now().isoformat(timespec="minutes"))
            if changed["result"] != issue.get("result"):
                qr.checks = [dict(c, result=changed["result"], reason=changed["reason"]) if c.get("issue") == iid else c
                             for c in qr.checks or []]
    except ReviewError as error:
        raise HTTPException(400, str(error)) from error
    qr.issues = [changed if i["id"] == iid else i for i in qr.issues]
    refresh(qr)
    audit.record(db, user, f"qc_{operation}", "qc_run", rid,
                 {"issue": iid, "review": changed.get("review"), "reference_doc": changed.get("reference", {}).get("doc"),
                  "severity": changed["severity"], "note": changed.get("review_note", ""),
                  "check_answer": changed.get("check_answer"), "bundle_answer": changed.get("bundle_answer")}, client_ip(request))
    db.commit()
    return RedirectResponse(f"/qc/{rid}#issue-{iid}", status_code=303)


@router.post("/qc/{rid}/review/bulk", dependencies=[Depends(verify_csrf)])
def qc_review_bulk(request: Request, rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    qr = _run(db, user, rid)
    if not _can_edit(user, qr):
        raise HTTPException(403)
    ids = []
    updated = []
    decision = {"action": "confirmed", "severity": "", "note": "규칙 후보 일괄 확정"}
    for issue in qr.issues:
        if (issue.get("confidence") == "rule" and issue.get("review") == "candidate"
                and issue.get("result") != "needs_check" and not issue.get("advice") and not issue.get("merged_into")):
            issue = transition(issue, decision)
            issue.update(reviewed_by=user.id, reviewed_at=now().isoformat(timespec="minutes"))
            ids.append(issue["id"])
        updated.append(issue)
    if ids:
        qr.issues = updated
        refresh(qr)
        audit.record(db, user, "qc_review_bulk", "qc_run", rid, {"issues": ids, "note": decision["note"]}, client_ip(request))
        db.commit()
    return RedirectResponse(f"/qc/{rid}", status_code=303)


@router.get("/qc/{rid}")
def qc_run_page(request: Request, rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    qr = _run(db, user, rid)
    config = criteria(db)
    cats = config.get("categories", {})
    by_cat: dict[str, int] = {}
    for i in qr.issues:
        by_cat[i["category"]] = by_cat.get(i["category"], 0) + 1
    can_fix = _can_edit(user, qr)
    return render(request, "qc/run.html", db, user, qr=qr, cats=cats, by_cat=by_cat, can_fix=can_fix,
                  fixable={i["id"] for i in qcs.fixable_issues(qr)}, check_options=CHECK_OPTIONS,
                  detectors=detector_rows(qr.ai_used, db=db), scoring=config.get("scoring", {}))


def _saved_answers(qr: QCRun) -> Answers:
    profile = qr.profile or {}
    return {key: profile[key] for key in Answers.__annotations__
            if key in profile and (profile.get("source", {}).get(key) == "user" or key == "bundle_answers")}


@router.post("/qc/{rid}/profile", dependencies=[Depends(verify_csrf)])
async def qc_profile_update(request: Request, rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    qr = _run(db, user, rid)
    if not _can_edit(user, qr):
        raise HTTPException(403)
    form = await request.form()
    previous = _saved_answers(qr)
    answers = {**_qc_profile(form), "bundle_answers": previous.get("bundle_answers", {})}
    try:
        new = qcs.rerun_saved(db, user, qr, profile=answers, ip=client_ip(request))
    except qcs.QCError as error:
        raise HTTPException(400, str(error)) from error
    db.commit()
    return RedirectResponse(f"/qc/{new.id}", status_code=303)


@router.get("/qc/{rid}/report.docx")
def qc_report(rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from ..qc.report import render_report

    return Response(render_report(_run(db, user, rid)),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="qc-{rid}.docx"'})


@router.post("/qc/{rid}/autofix", dependencies=[Depends(verify_csrf)])
async def qc_autofix(request: Request, rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    qr = _run(db, user, rid)
    if not (qr.created_by == user.id or (qr.protocol is not None and wf.is_member(user, qr.protocol))):
        raise HTTPException(403)
    form = await request.form()
    ids = [int(x) for x in form.getlist("issue") if str(x).isdigit()]
    font = str(form.get("font", "")).strip() if form.get("unify_font") == "on" else None
    try:
        outs = qcs.apply_fixes(db, user, qr, ids, font=font or None, save_version=form.get("save") != "off",
                               ip=client_ip(request))
    except qcs.QCError as e:
        flash(request, str(e), "error")
        return RedirectResponse(f"/qc/{rid}", status_code=303)
    db.commit()
    done = sum(o.get("applied", 0) for o in outs)
    saved = [o for o in outs if o.get("version")]
    flash(request, f"자동 교정 {done}곳 적용 - 파일 {len(outs)}건"
          + (f", 과제 서류 새 버전 {len(saved)}건 저장(재점검하십시오)" if saved else ""), "success")
    for o in outs:
        if o.get("error") or o.get("save_error"):
            flash(request, f"{o['name']}: {o.get('error') or o.get('save_error')}", "error")
    return RedirectResponse(f"/qc/{rid}#outputs", status_code=303)


@router.get("/qc/{rid}/file/{index}/{kind}")
def qc_file(rid: int, index: int, kind: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    qr = _run(db, user, rid)
    outs = qr.outputs or []
    if not (0 <= index < len(outs)) or kind not in ("clean", "tracked") or kind not in outs[index]:
        raise HTTPException(404)
    meta = outs[index][kind]
    path = Path(meta["path"])
    if not path.is_file():
        raise HTTPException(404, "파일이 없습니다.")
    return Response(path.read_bytes(), media_type="application/octet-stream",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(meta['filename'])}"})


@router.post("/qc/{rid}/rerun", dependencies=[Depends(verify_csrf)])
def qc_rerun(request: Request, rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    qr = _run(db, user, rid)
    if qr.protocol is None:
        flash(request, "빠른 점검은 교정본을 다시 올려 점검하십시오.", "info")
        return RedirectResponse(f"/qc/{rid}", status_code=303)
    wf.require_member(user, qr.protocol)
    answers = _saved_answers(qr)
    new = (qcs.rerun_saved(db, user, qr, profile=answers, ip=client_ip(request))
           if answers.get("compare_only") else
           qcs.run_for_protocol(db, user, qr.protocol, use_ai=qr.ai_used, ip=client_ip(request), profile=answers))
    db.commit()
    return RedirectResponse(f"/qc/{new.id}", status_code=303)


# ------------------------------------------------------------------ 보완 대응
@router.get("/protocols/{pid}/revision")
def revision_page(request: Request, pid: int, draft: int = 0, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    items = wf.open_revision_items(db, p)
    drafts = rv.draft_responses(p, items, use_ai=bool(draft) and _ai_on(), db=db, user=user) if items else {}
    if draft:
        db.commit()
    return render(request, "package/revision.html", db, user, p=p, items=items, drafts=drafts,
                  is_member=wf.is_member(user, p), ai_on=_ai_on(), drafted=bool(draft), field_labels=_field_labels())


def _field_labels() -> dict[str, str]:
    from ..workflow.diff import FIELD_LABELS

    return {**{k: v[0] for k, v in FIELD_LABELS.items()}, **DRAFT_FIELDS}
