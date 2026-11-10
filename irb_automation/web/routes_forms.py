"""기관 양식 화면: 기관 프로필, 양식 정의(버전·미리보기·활성화), 양식 팩 가져오기/내보내기, 웹 작성 서식."""
from __future__ import annotations

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, esign
from ..database import get_db
from ..forms import online as of
from ..forms.logic import FIELD_TYPES
from ..forms.models import FormDefinition, OnlineFormResponse
from ..forms.packs import bundled_packs, export_pack, import_pack, parse_pack, read_bundled, seed_forms
from ..forms.profile import format_protocol_no, get_profile, update_profile, validate_format
from ..forms.schema import (
    PREFILL_SOURCES,
    SchemaError,
    activate_definition,
    deactivate_definition,
    get_online_spec,
    kind_label,
    parse_form_values,
    parse_sections,
    save_definition,
    validate_schema,
)
from ..models import Protocol, Role, Signature, User
from ..timeutil import today
from ..workflow import service as wf
from .deps import client_ip, current_user, flash, render, require_roles, verify_csrf

router = APIRouter()
form_admin = require_roles(Role.ADMIN)  # 관리자 포함
ONLINE_PREFIX = "f__"


# ===================================================================== 기관 프로필
@router.get("/admin/profile")
def profile_page(request: Request, user: User = Depends(form_admin), db: Session = Depends(get_db)):
    p = get_profile(db)
    db.commit()
    sample = format_protocol_no(p.protocol_no_format, today().year, 1)
    return render(request, "forms/admin_profile.html", db, user, p=p, sample=sample, errors=[])


@router.post("/admin/profile", dependencies=[Depends(verify_csrf)])
async def profile_save(request: Request, user: User = Depends(form_admin), db: Session = Depends(get_db)):
    form = await request.form()
    data = {k: str(form.get(k, "")) for k in ("institution_name", "irb_name", "protocol_no_format", "letter_footer", "contact")}
    errors = update_profile(db, user, data, client_ip(request))
    if errors:
        db.rollback()
        p = get_profile(db)
        for k, v in data.items():
            setattr(p, k, v)
        sample = format_protocol_no(data["protocol_no_format"], today().year, 1) if not validate_format(data["protocol_no_format"]) else "-"
        db.expunge(p)
        return render(request, "forms/admin_profile.html", db, user, p=p, sample=sample, errors=errors, status_code=400)
    db.commit()
    flash(request, "기관 프로필을 저장했습니다. 다음 접수 과제부터 새 과제번호 형식이 적용됩니다.", "success")
    return RedirectResponse("/admin/profile", status_code=303)


# ===================================================================== 양식 정의
@router.get("/admin/forms")
def forms_page(request: Request, user: User = Depends(form_admin), db: Session = Depends(get_db)):
    if seed_forms(db):
        db.commit()
    defs = db.scalars(select(FormDefinition).order_by(FormDefinition.kind, FormDefinition.version.desc())).all()
    groups: dict[str, list[FormDefinition]] = {}
    for d in defs:
        groups.setdefault(d.kind, []).append(d)
    return render(request, "forms/admin_forms.html", db, user, groups=groups, packs=bundled_packs(),
                  kind_label=kind_label)


def _editor(request, db, user, *, kind, name, text, note="", errors=None, preview=None, base=None, status=200):
    return render(request, "forms/admin_form_edit.html", db, user, kind=kind, form_name=name, text=text, note=note,
                  errors=errors or [], preview=preview, base=base, field_types=FIELD_TYPES,
                  prefill_sources=PREFILL_SOURCES, kind_label=kind_label, status_code=status)


def _dump(schema: dict) -> str:
    return yaml.safe_dump(schema, allow_unicode=True, sort_keys=False, width=120)


@router.get("/admin/forms/new")
def form_new(request: Request, kind: str = "protocol", user: User = Depends(form_admin), db: Session = Depends(get_db)):
    if kind.startswith("online:"):
        schema = {"label": "새 웹 서식", "per_member": False, "intro": "", "pledge": "위 내용이 사실임을 확인합니다.",
                  "sections": [{"title": "작성 항목", "fields": [{"key": "answer", "label": "내용", "type": "textarea", "required": True}]}]}
    else:
        schema = {"sections": [{"title": "기관 추가 항목", "fields": [{"key": "example_item", "label": "예시 항목", "type": "text"}]}]}
    return _editor(request, db, user, kind=kind, name="", text=_dump(schema))


@router.get("/admin/forms/{fid}/edit")
def form_edit(request: Request, fid: int, user: User = Depends(form_admin), db: Session = Depends(get_db)):
    d = db.get(FormDefinition, fid)
    if d is None:
        raise HTTPException(404)
    return _editor(request, db, user, kind=d.kind, name=d.name, text=_dump(d.schema), base=d)


def _load_text(text: str) -> tuple[dict | None, list[str]]:
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as e:
        return None, [f"YAML 문법 오류: {e}"]
    return data, []


@router.post("/admin/forms/save", dependencies=[Depends(verify_csrf)])
async def form_save(request: Request, user: User = Depends(form_admin), db: Session = Depends(get_db)):
    form = await request.form()
    kind, name, text = str(form.get("kind", "")).strip(), str(form.get("name", "")), str(form.get("schema", ""))
    note = str(form.get("note", ""))
    schema, errors = _load_text(text)
    preview_only = form.get("action") == "preview"
    if schema is not None:
        errors = validate_schema(kind, schema)
    if errors or preview_only:
        preview = parse_sections(schema) if (schema is not None and not errors) else None
        return _editor(request, db, user, kind=kind, name=name, text=text, note=note, errors=errors,
                       preview=preview, status=400 if errors else 200)
    try:
        d = save_definition(db, user, kind, name, schema, activate=form.get("activate") == "on", note=note,
                            source="편집", ip=client_ip(request))
    except SchemaError as e:
        db.rollback()
        return _editor(request, db, user, kind=kind, name=name, text=text, note=note, errors=e.errors, status=400)
    db.commit()
    flash(request, f"{kind_label(kind, schema)} v{d.version}을(를) 저장했습니다{' (활성화)' if d.active else ''}.", "success")
    return RedirectResponse("/admin/forms", status_code=303)


@router.get("/admin/forms/{fid}/preview")
def form_preview(request: Request, fid: int, user: User = Depends(form_admin), db: Session = Depends(get_db)):
    d = db.get(FormDefinition, fid)
    if d is None:
        raise HTTPException(404)
    return render(request, "forms/admin_form_preview.html", db, user, d=d, sections=parse_sections(d.schema),
                  kind_label=kind_label)


@router.post("/admin/forms/{fid}/activate", dependencies=[Depends(verify_csrf)])
def form_activate(request: Request, fid: int, user: User = Depends(form_admin), db: Session = Depends(get_db)):
    d = db.get(FormDefinition, fid)
    if d is None:
        raise HTTPException(404)
    activate_definition(db, user, d, client_ip(request))
    db.commit()
    flash(request, f"{d.name} v{d.version}을(를) 활성화했습니다.", "success")
    return RedirectResponse("/admin/forms", status_code=303)


@router.post("/admin/forms/{fid}/deactivate", dependencies=[Depends(verify_csrf)])
def form_deactivate(request: Request, fid: int, user: User = Depends(form_admin), db: Session = Depends(get_db)):
    d = db.get(FormDefinition, fid)
    if d is None:
        raise HTTPException(404)
    deactivate_definition(db, user, d, client_ip(request))
    db.commit()
    flash(request, f"{d.name}을(를) 비활성화했습니다.", "success")
    return RedirectResponse("/admin/forms", status_code=303)


@router.get("/admin/forms/export")
def forms_export(request: Request, user: User = Depends(form_admin), db: Session = Depends(get_db)):
    text = export_pack(db)
    audit.record(db, user, "form_pack_export", "form_pack", None, {}, client_ip(request))
    db.commit()
    return Response(text.encode("utf-8"), media_type="application/x-yaml; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=irb_form_pack.yaml"})


@router.post("/admin/forms/import", dependencies=[Depends(verify_csrf)])
async def forms_import(request: Request, user: User = Depends(form_admin), db: Session = Depends(get_db)):
    form = await request.form()
    upload = form.get("file")
    try:
        if hasattr(upload, "read") and getattr(upload, "filename", ""):
            pack = parse_pack(await upload.read())
        elif form.get("bundled"):
            pack = parse_pack(read_bundled(str(form.get("bundled"))))
        else:
            raise SchemaError(["가져올 파일을 선택하거나 기본 제공 팩을 고르십시오."])
        result = import_pack(db, user, pack, activate=form.get("activate") == "on",
                             apply_profile=form.get("apply_profile") == "on", ip=client_ip(request))
    except SchemaError as e:
        db.rollback()
        flash(request, "양식 팩 오류: " + " / ".join(e.errors[:8]), "error")
        return RedirectResponse("/admin/forms", status_code=303)
    db.commit()
    flash(request, f"양식 팩 '{result['source']}'에서 {len(result['forms'])}개 양식을 가져왔습니다"
                   f"{' (기관 프로필 적용)' if result['profile'] else ''}.", "success")
    return RedirectResponse("/admin/forms", status_code=303)


# ===================================================================== 웹 작성 서식
def _protocol(db: Session, user: User, pid: int) -> Protocol:
    p = db.get(Protocol, pid)
    if p is None:
        raise HTTPException(404, "과제를 찾을 수 없습니다.")
    if not wf.can_view(db, user, p):
        raise HTTPException(403, "이 과제를 열람할 권한이 없습니다.")
    return p


def _spec(db: Session, doc_type: str):
    spec = get_online_spec(db, doc_type)
    if spec is None:
        raise HTTPException(404, "활성화된 웹 서식이 아닙니다.")
    return spec


@router.get("/protocols/{pid}/online-forms")
def online_list(request: Request, pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    if seed_forms(db):
        db.commit()
    status = of.form_status(db, p)
    for st in status:
        st["can_fill"], st["reason"] = of.can_fill(user, p, st["spec"])
    return render(request, "forms/online_list.html", db, user, p=p, status=status, labels=of.STATUS_LABELS)


def _fill_context(db, user, p, spec, target: User | None):
    r = of.find_response(db, p, spec, target)
    can, reason = of.can_fill(user, p, spec)
    own = target is not None and target.id == user.id
    editable = can and (own or not spec.per_member)
    return r, editable, reason


@router.get("/protocols/{pid}/online-forms/{doc_type}")
def online_fill(request: Request, pid: int, doc_type: str, user_id: int | None = None,
                user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    spec = _spec(db, doc_type)
    target = db.get(User, user_id) if user_id else user
    if target is None:
        raise HTTPException(404)
    r, editable, reason = _fill_context(db, user, p, spec, target)
    values = dict(r.answers) if r else of.prefill_values(spec, target, p)
    return render(request, "forms/online_fill.html", db, user, p=p, spec=spec, r=r, target=target, values=values,
                  errors={}, editable=editable, reason=reason, labels=of.STATUS_LABELS, prefix=ONLINE_PREFIX,
                  cur_status=of.entry_status(r))


@router.post("/protocols/{pid}/online-forms/{doc_type}/{action}", dependencies=[Depends(verify_csrf)])
async def online_post(request: Request, pid: int, doc_type: str, action: str,
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    if action not in ("save", "submit"):
        raise HTTPException(404)
    p = _protocol(db, user, pid)
    spec = _spec(db, doc_type)
    form = await request.form()
    answers, parse_errors = parse_form_values(form, spec.fields, ONLINE_PREFIX)
    back = f"/protocols/{pid}/online-forms/{doc_type}"
    try:
        if action == "save":
            of.save_draft(db, user, p, spec, answers, client_ip(request))
            db.commit()
            flash(request, "임시저장했습니다. 서명 제출해야 서류로 인정됩니다.", "success")
            return RedirectResponse(back, status_code=303)
        if parse_errors:
            raise of.OnlineFormInvalid(parse_errors)
        r, doc = of.submit(db, user, p, spec, answers, str(form.get("password", "")), form.get("attest") == "on",
                           client_ip(request))
    except of.OnlineFormInvalid as e:
        db.commit()  # 입력값은 임시저장
        r = of.find_response(db, p, spec, user)
        return render(request, "forms/online_fill.html", db, user, p=p, spec=spec, r=r, target=user, values=answers,
                      errors=e.errors, editable=True, reason="", labels=of.STATUS_LABELS, prefix=ONLINE_PREFIX,
                      cur_status=of.entry_status(r), status_code=400)
    except (of.OnlineFormError, esign.SignatureError) as e:
        db.rollback()
        flash(request, str(e), "error")
        return RedirectResponse(back, status_code=303)
    db.commit()
    flash(request, f"'{spec.label}'을(를) 전자서명으로 제출했습니다(v{r.version}). 서류 '{doc.filename}'이(가) 첨부되었습니다.",
          "success")
    return RedirectResponse(f"/protocols/{pid}/online-forms", status_code=303)


@router.get("/protocols/{pid}/online-forms/{doc_type}/print")
def online_print(request: Request, pid: int, doc_type: str, response_id: int | None = None,
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _protocol(db, user, pid)
    spec = _spec(db, doc_type)
    r = db.get(OnlineFormResponse, response_id) if response_id else of.find_response(db, p, spec, user)
    if r is None or r.protocol_id != p.id or r.doc_type != doc_type:
        raise HTTPException(404, "작성된 서식이 없습니다.")
    sig = db.get(Signature, r.signature_id) if r.signature_id else None
    return HTMLResponse(of.render_print(db, p, spec, r, sig))
