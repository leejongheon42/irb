"""웹 작성 온라인 서식: 브라우저에서 작성 → 임시저장 → 전자서명 제출 → 인쇄용 문서 자동 첨부.

- 과제 단위 서식: 과제당 1부(연구진 누구나, 또는 signer: pi 이면 연구책임자만)
- 개인별 서식(per_member): 연구책임자·공동연구자 각자 1부. 제출할 때마다 본인 명의 문서가 첨부된다.
  체크리스트에서는 **연구진 전원이 제출했을 때만** 해당 서류가 제출된 것으로 본다(일부만 제출된 동안은 '미제출').
  같은 종류의 서류를 파일로 업로드했다면(종이 서식 스캔 등) 업로드본이 우선 인정된다.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit, esign
from ..config import get_settings
from ..models import Document, Protocol, User
from ..timeutil import now
from .logic import display, is_visible, validate_values
from .models import OnlineFormResponse
from .schema import OnlineFormSpec, active_online_forms


class OnlineFormError(Exception):
    pass


class OnlineFormInvalid(OnlineFormError):
    def __init__(self, errors: dict[str, str]):
        super().__init__(f"입력 오류 {len(errors)}건")
        self.errors = errors


# ------------------------------------------------------------------ 조회
def team(db: Session, protocol: Protocol) -> list[User]:
    users = [protocol.pi or db.get(User, protocol.pi_id)]
    for uid in protocol.co_investigators or []:
        u = db.get(User, int(uid))
        if u is not None and u.id != protocol.pi_id:
            users.append(u)
    return [u for u in users if u is not None]


def responses_for(db: Session, protocol: Protocol, doc_type: str | None = None) -> list[OnlineFormResponse]:
    q = select(OnlineFormResponse).where(OnlineFormResponse.protocol_id == protocol.id)
    if doc_type:
        q = q.where(OnlineFormResponse.doc_type == doc_type)
    return list(db.scalars(q.order_by(OnlineFormResponse.id)))


def find_response(db: Session, protocol: Protocol, spec: OnlineFormSpec, user: User | None) -> OnlineFormResponse | None:
    rows = responses_for(db, protocol, spec.doc_type)
    if spec.per_member:
        return next((r for r in rows if user is not None and r.user_id == user.id), None)
    return rows[0] if rows else None


def entry_status(r: OnlineFormResponse | None) -> str:
    if r is None:
        return "missing"
    if r.version >= 1 and r.status == "submitted":
        return "submitted"
    if r.version >= 1:
        return "editing"  # 제출본은 유효, 수정 중
    return "draft"


STATUS_LABELS = {"missing": "미작성", "draft": "임시저장", "editing": "제출됨(수정 중)", "submitted": "제출됨"}


def form_status(db: Session, protocol: Protocol) -> list[dict[str, Any]]:
    out = []
    members = team(db, protocol)
    for spec in active_online_forms(db):
        rows = responses_for(db, protocol, spec.doc_type)
        if spec.per_member:
            by_user = {r.user_id: r for r in rows}
            entries = [{"user": u, "response": by_user.get(u.id), "status": entry_status(by_user.get(u.id))}
                       for u in members]
        else:
            r = rows[0] if rows else None
            entries = [{"user": r.user if r else None, "response": r, "status": entry_status(r)}]
        submitted = sum(1 for e in entries if e["status"] in ("submitted", "editing"))
        required = len(entries)
        out.append({
            "spec": spec,
            "entries": entries,
            "submitted": submitted,
            "required": required,
            "complete": required > 0 and submitted == required,
        })
    return out


def pending_online_forms(db: Session, protocol: Protocol) -> tuple[list[dict[str, Any]], set[str]]:
    """사전검토용: (미완료 웹 서식 목록, '미제출'로 간주할 서류 종류).

    업로드된(웹 작성이 아닌) 같은 종류의 서류가 있으면 그 서식은 완료로 본다.
    """
    status = form_status(db, protocol)
    if not status:
        return [], set()
    web_doc_ids = {r.document_id for r in responses_for(db, protocol) if r.document_id}
    offline_types = {d.doc_type for d in protocol.documents if d.id not in web_doc_ids}
    pending: list[dict[str, Any]] = []
    incomplete: set[str] = set()
    for st in status:
        spec = st["spec"]
        if st["complete"] or spec.doc_type in offline_types:
            continue
        started = st["submitted"] > 0
        if not (spec.required or (spec.per_member and started)):
            continue
        missing = [e["user"].full_name for e in st["entries"]
                   if e["user"] is not None and e["status"] not in ("submitted", "editing")]
        pending.append({"doc_type": spec.doc_type, "label": spec.label, "missing": missing,
                        "required": spec.required, "partial": started, "per_member": spec.per_member})
        incomplete.add(spec.doc_type)
    return pending, incomplete


# ------------------------------------------------------------------ 권한
def can_fill(user: User, protocol: Protocol, spec: OnlineFormSpec) -> tuple[bool, str]:
    if user.id not in protocol.member_ids():
        return False, "연구책임자·공동연구자만 작성할 수 있습니다."
    if not protocol.is_editable:
        return False, f"'{protocol.status.label}' 상태에서는 작성할 수 없습니다(작성중·보완요청·변경작성 상태에서 가능)."
    if spec.signer == "pi" and user.id != protocol.pi_id:
        return False, "연구책임자만 작성할 수 있는 서식입니다."
    return True, ""


def prefill_values(spec: OnlineFormSpec, user: User, protocol: Protocol) -> dict[str, Any]:
    sources = {
        "user.full_name": user.full_name,
        "user.department": user.department,
        "user.email": user.email,
        "pi.full_name": protocol.pi.full_name if protocol.pi else "",
        "protocol.title": protocol.title,
        "protocol.protocol_no": protocol.protocol_no or "",
        "protocol.funding_source": protocol.funding_source,
        "today": date.today().isoformat(),
    }
    return {f.key: sources[f.prefill] for f in spec.fields if f.prefill and sources.get(f.prefill)}


# ------------------------------------------------------------------ 작성·제출
def _get_or_create(db: Session, user: User, protocol: Protocol, spec: OnlineFormSpec) -> OnlineFormResponse:
    r = find_response(db, protocol, spec, user)
    if r is None:
        r = OnlineFormResponse(protocol_id=protocol.id, doc_type=spec.doc_type, user_id=user.id, answers={},
                               status="draft", version=0)
        db.add(r)
    elif not spec.per_member:
        r.user_id = user.id  # 과제 단위 서식은 마지막 작성자 명의
    r.definition_id = spec.definition_id
    return r


def save_draft(db: Session, user: User, protocol: Protocol, spec: OnlineFormSpec, answers: dict[str, Any],
               ip: str = "") -> OnlineFormResponse:
    ok, reason = can_fill(user, protocol, spec)
    if not ok:
        raise OnlineFormError(reason)
    r = _get_or_create(db, user, protocol, spec)
    r.answers = answers
    if r.version >= 1:
        r.status = "draft"
    r.updated_at = now()
    db.flush()
    audit.record(db, user, "online_form_save", "protocol", protocol.id, {"doc_type": spec.doc_type}, ip)
    return r


def submit(
    db: Session,
    user: User,
    protocol: Protocol,
    spec: OnlineFormSpec,
    answers: dict[str, Any],
    password: str,
    attest: bool,
    ip: str = "",
) -> tuple[OnlineFormResponse, Document]:
    ok, reason = can_fill(user, protocol, spec)
    if not ok:
        raise OnlineFormError(reason)
    errors = validate_values(spec.fields, answers)
    if errors:
        save_draft(db, user, protocol, spec, answers, ip)
        raise OnlineFormInvalid(errors)
    if not attest:
        raise OnlineFormError("서약 내용에 동의해야 제출할 수 있습니다.")
    r = _get_or_create(db, user, protocol, spec)
    r.answers = answers
    db.flush()
    payload = {
        "protocol_id": protocol.id,
        "protocol_no": protocol.protocol_no,
        "doc_type": spec.doc_type,
        "definition": {"id": spec.definition_id, "version": spec.version},
        "user_id": user.id,
        "answers": answers,
    }
    sig = esign.sign(db, user, password, f"웹 서식 제출: {spec.label}", "online_form", r.id, payload, ip)
    r.version += 1
    r.status = "submitted"
    r.submitted_at = now()
    r.signature_id = sig.id
    doc = _attach_document(db, user, protocol, spec, r, sig)
    r.document_id = doc.id
    db.flush()
    audit.record(db, user, "online_form_submit", "protocol", protocol.id,
                 {"doc_type": spec.doc_type, "version": r.version, "document_id": doc.id, "hash": sig.content_hash}, ip)
    return r, doc


def visible_rows(spec: OnlineFormSpec, answers: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for sec in spec.sections:
        items = [(f, display(f, answers.get(f.key))) for f in sec["fields"] if is_visible(f, answers)]
        if items:
            rows.append({"title": sec["title"], "rows": items})
    return rows


def render_print(db: Session, protocol: Protocol, spec: OnlineFormSpec, r: OnlineFormResponse, sig: Any = None) -> str:
    from ..engine.ruleset import get_ruleset
    from ..web.deps import templates
    from .profile import SettingsView, profile_values

    tpl = templates.env.get_template("forms/online_print.html")
    return tpl.render(
        settings=SettingsView(get_settings(), profile_values(db)), rs=get_ruleset(), today=date.today(),
        spec=spec, p=protocol, r=r, respondent=r.user or db.get(User, r.user_id), sig=sig,
        sections=visible_rows(spec, r.answers or {}),
    )


def _attach_document(db: Session, user: User, protocol: Protocol, spec: OnlineFormSpec, r: OnlineFormResponse,
                     sig: Any) -> Document:
    r.user = user
    html = render_print(db, protocol, spec, r, sig).encode("utf-8")
    settings = get_settings()
    folder = settings.upload_dir / str(protocol.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}_{spec.doc_type}_web.html"
    path.write_bytes(html)
    version = 1 + max((d.version for d in protocol.documents if d.doc_type == spec.doc_type), default=0)
    who = f"_{user.full_name}" if spec.per_member else ""
    doc = Document(
        protocol_id=protocol.id, doc_type=spec.doc_type, filename=f"{spec.label}(웹작성){who}_v{r.version}.html",
        stored_path=str(path), sha256=hashlib.sha256(html).hexdigest(), size=len(html), version=version,
        uploaded_by=user.id,
    )
    db.add(doc)
    db.flush()
    from ..submission.hooks import finalize_system_document

    finalize_system_document(db, user, doc, "online_form", versioned=not spec.per_member)
    db.expire(protocol, ["documents"])
    return doc
