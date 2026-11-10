"""제출 서류 패키지: 이번 제출(신규·보완·변경·지속·종료)에 필요한 서류를 정하고, 자동으로 완성·점검·묶는다.

  plan()          : 필요 서류 목록과 각 서류의 준비 방법(자동 작성 / 웹 서식 / 직접 업로드)·상태·점검 결과
  autocomplete()  : ① 비어 있는 서술 항목 초안 작성 → ② 자동 작성 가능한 서류 생성(기관 서식 우선, DOCX)
                    → ③ 전체 서류 품질 점검(교차 대조·버전·필수항목·용어·오탈자) 을 한 번에 실행
  build_zip()     : e-IRB에 바로 올릴 수 있게 번호·서류명·버전으로 이름 붙인 ZIP(+ 제출 목록·점검 결과)
"""
from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import audit
from ..documents import generatable_doc_types, generate_and_attach
from ..engine.ruleset import get_ruleset
from ..models import Document, Protocol, ProtocolStatus, SubmissionKind, User
from ..submission.validate import safe_filename
from ..submission.versions import latest_by_type
from ..timeutil import now
from ..workflow import service as wf

log = logging.getLogger(__name__)
S = ProtocolStatus

PERSONAL_DOCS = {
    "cv": "연구책임자가 직접 작성·서명한 이력서를 올리십시오(기관 양식이 있으면 서식 자료실에서 내려받기).",
    "training_cert": "연구윤리 교육 이수증(PDF)을 교육 기관 사이트에서 내려받아 올리십시오.",
    "external_irb_approval": "대표 기관 IRB 승인서를 올리십시오.",
    "transfer_agreement": "기관 간 자료 제공 계약서를 올리십시오.",
    "budget": "연구비 예산서를 올리십시오.",
}


def _label(doc_type: str) -> str:
    return get_ruleset().label("document_types", doc_type)


def submission_kind(protocol: Protocol, kind: str | None = None) -> SubmissionKind:
    if kind:
        return SubmissionKind(kind)
    try:
        return wf.next_submission_kind(protocol)
    except wf.WorkflowError:
        return SubmissionKind.CONTINUING if protocol.status in (S.APPROVED, S.EXPIRED) else SubmissionKind.INITIAL


def plan(db: Session, protocol: Protocol, kind: str | None = None) -> dict[str, Any]:
    k = submission_kind(protocol, kind)
    result = wf.preview(db, protocol, kind=k.value)
    gen = generatable_doc_types(db)
    latest = latest_by_type(list(protocol.documents))
    online_types: dict[str, dict] = {}
    try:
        from ..forms.online import pending_online_forms

        pending, _ = pending_online_forms(db, protocol)
        online_types = {x["doc_type"]: x for x in pending}
    except Exception:  # 기관 양식 모듈 오류는 패키지 화면을 막지 않는다
        log.exception("online forms status failed")
    wanted: list[tuple[str, bool, str]] = [(c["doc"], True, c.get("condition", "")) for c in result["checklist"]]
    if protocol.status == S.REVISION_REQUESTED:
        wanted.insert(0, ("revision_response", True, "보완 요청 답변"))
    if k == SubmissionKind.AMENDMENT:
        wanted.insert(0, ("amendment_table", True, "변경심의"))
    seen: set[str] = set()
    items = []
    for doc_type, required, cond in wanted:
        if doc_type in seen:
            continue
        seen.add(doc_type)
        cur = latest.get(doc_type)
        how = ("online" if doc_type in online_types else "generate" if doc_type in gen
               else "upload")
        stale = bool(cur and cur.source in ("generated",) and protocol.updated_at and cur.uploaded_at
                     and cur.uploaded_at < protocol.updated_at)
        qc = (cur.validation or {}).get("qc") if cur else None
        status = "missing" if cur is None else ("stale" if stale else
                                                ("issues" if qc and (qc.get("critical") or qc.get("major")) else
                                                 ("checked" if qc else "ready")))
        items.append({
            "doc_type": doc_type, "label": _label(doc_type), "required": required, "condition": cond,
            "how": how, "template": gen.get(doc_type), "current": cur, "stale": stale, "qc": qc, "status": status,
            "hint": PERSONAL_DOCS.get(doc_type, "") if how == "upload" else
            (f"웹 작성 서식 미완료: {', '.join(online_types[doc_type]['missing']) or '작성 필요'}" if how == "online" else ""),
        })
    for doc_type, cur in latest.items():  # 체크리스트 밖에서 올린 서류
        if doc_type not in seen and cur.has_file:
            qc = (cur.validation or {}).get("qc")
            items.append({"doc_type": doc_type, "label": _label(doc_type), "required": False, "condition": "추가 서류",
                          "how": "upload", "template": gen.get(doc_type), "current": cur, "stale": False, "qc": qc,
                          "status": "checked" if qc else "ready", "hint": ""})
    done = sum(1 for i in items if i["required"] and i["current"] is not None and not i["stale"])
    req = sum(1 for i in items if i["required"])
    from ..qc.models import QCRun

    last_qc = db.scalars(select(QCRun).where(QCRun.protocol_id == protocol.id).order_by(QCRun.id.desc())).first()
    from .drafting import empty_fields

    return {
        "kind": k, "items": items, "required": req, "done": done,
        "progress": int(round(100 * done / req)) if req else 100,
        "prereview": result, "last_qc": last_qc,
        "empty_fields": empty_fields(protocol) if protocol.is_editable else [],
        "can_generate": [i["doc_type"] for i in items if i["how"] == "generate" and (i["current"] is None or i["stale"])],
    }


def autocomplete(db: Session, user: User, protocol: Protocol, *, use_ai: bool = True, draft_fields: bool = True,
                 regenerate: bool = False, kind: str | None = None, ip: str = "") -> dict[str, Any]:
    """빈 항목 초안 → 서류 자동 작성 → 품질 점검. 각 단계의 결과를 돌려준다."""
    wf.require_member(user, protocol)
    out: dict[str, Any] = {"drafted": {}, "generated": [], "failed": [], "qc_run": None, "messages": []}
    if draft_fields and protocol.is_editable:
        from .drafting import draft

        d = draft(protocol, use_ai=use_ai, db=db, user=user)
        if d["drafts"]:
            wf.update_protocol(db, user, protocol, {k: v["text"] for k, v in d["drafts"].items()}, ip)
            out["drafted"] = {k: v["source"] for k, v in d["drafts"].items()}
        out["messages"].append(f"서술 항목 초안: {len(d['drafts'])}개 ({d['message']})")
        db.flush()
        db.refresh(protocol)
    pl = plan(db, protocol, kind)
    targets = [i for i in pl["items"] if i["how"] == "generate"
               and (regenerate or i["current"] is None or i["stale"] or bool(out["drafted"]))]
    if protocol.is_editable:
        for it in targets:
            try:
                doc = generate_and_attach(db, user, protocol, it["doc_type"], ip)
                out["generated"].append({"doc_type": it["doc_type"], "label": it["label"], "filename": doc.filename,
                                         "version": doc.version, "source": it["template"]})
            except Exception as e:  # 서류 하나의 실패가 나머지를 막지 않게
                log.exception("generate %s failed", it["doc_type"])
                out["failed"].append({"doc_type": it["doc_type"], "label": it["label"], "error": str(e)})
        db.flush()
        db.refresh(protocol)
    elif targets:
        out["messages"].append("현재 상태에서는 서류를 새로 작성하지 않았습니다(작성중·보완요청 받음·변경 준비중에만 가능).")
    from ..qc.service import QCError, run_for_protocol

    try:
        qr = run_for_protocol(db, user, protocol, use_ai=use_ai, ip=ip)
        out["qc_run"] = qr
    except QCError as e:
        out["messages"].append(str(e))
    audit.record(db, user, "package_autocomplete", "protocol", protocol.id,
                 {"drafted": sorted(out["drafted"]), "generated": [g["doc_type"] for g in out["generated"]],
                  "failed": [f["doc_type"] for f in out["failed"]],
                  "qc": out["qc_run"].id if out["qc_run"] else None}, ip)
    return out


def build_zip(db: Session, user: User, protocol: Protocol, kind: str | None = None) -> tuple[bytes, str]:
    pl = plan(db, protocol, kind)
    buf = io.BytesIO()
    lines = [f"{protocol.title}", f"제출 구분: {pl['kind'].label}", f"작성일: {now():%Y-%m-%d %H:%M}", ""]
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for it in pl["items"]:
            d: Document | None = it["current"]
            mark = "필수" if it["required"] else "추가"
            if d is None or not d.has_file:
                lines.append(f"[미첨부] ({mark}) {it['label']}")
                continue
            n += 1
            ext = Path(d.filename).suffix or ".bin"
            name = safe_filename(f"{n:02d}_{it['label']}_v{d.version}{ext}")
            z.write(d.stored_path, name)
            qc = it["qc"] or {}
            qc_txt = (f" · 점검 C{qc.get('critical', 0)}/M{qc.get('major', 0)}/m{qc.get('minor', 0)}" if qc else "")
            lines.append(f"{n:02d}. ({mark}) {it['label']} v{d.version} - {d.filename}{qc_txt}")
        qr = pl["last_qc"]
        if qr is not None:
            lines += ["", f"최근 서류 점검: {qr.created_at:%Y-%m-%d %H:%M} · {qr.score}점 · {qr.verdict} "
                          f"(Critical {qr.counts.get('critical', 0)}, Major {qr.counts.get('major', 0)}, Minor {qr.counts.get('minor', 0)})"]
            for i in qr.issues:
                if i["severity"] in ("critical", "major"):
                    lines.append(f"  - [{i['severity']}] {i['doc_name']}: {i['title']}")
        z.writestr("00_제출서류_목록.txt", "\n".join(lines).encode("utf-8"))
    audit.record(db, user, "document_bundle", "protocol", protocol.id, {"files": n, "kind": pl["kind"].value})
    fname = safe_filename(f"IRB제출_{pl['kind'].label}_{protocol.title[:30]}.zip")
    return buf.getvalue(), fname
