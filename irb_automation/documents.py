"""제출 서류 자동 작성.

과제 입력 내용으로 심의의뢰서·면제 신청서·동의면제 사유서·가명처리 계획서·자료수집 항목표·
보안서약서·변경 대비표를 생성해 바로 첨부한다. 연구자는 내용을 확인한 뒤 서명본을 추가 첨부할 수 있다.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date
from typing import Any

from sqlalchemy.orm import Session

from . import audit
from .config import get_settings
from .deid import patterns as P
from .engine.facts import snapshot_protocol
from .engine.ruleset import get_ruleset
from .models import Document, Protocol, User
from .workflow import service as wf
from .workflow.diff import diff_snapshots

GENERATABLE: dict[str, str] = {
    "application": "generated/application.html",
    "exemption_request": "generated/exemption_request.html",
    "waiver_request": "generated/waiver_request.html",
    "deid_plan": "generated/deid_plan.html",
    "crf": "generated/crf.html",
    "security_pledge": "generated/security_pledge.html",
    "amendment_table": "generated/amendment_table.html",
    "site_list": "generated/site_list.html",
}

# 가명처리 계획서의 항목별 처리 기준(보건의료데이터 활용 가이드라인의 처리 방식 요약)
_HANDLING = {
    "rrn": ("직접 식별자", "삭제"),
    "foreigner_no": ("직접 식별자", "삭제"),
    "passport": ("직접 식별자", "삭제"),
    "name": ("직접 식별자", "삭제"),
    "mrn": ("직접 식별자", "연구용 일련번호(키 기반 일방향 암호화)로 대체, 매핑정보 분리 보관"),
    "phone": ("직접 식별자", "삭제"),
    "email": ("직접 식별자", "삭제"),
    "insurance_no": ("직접 식별자", "삭제"),
    "account_no": ("직접 식별자", "삭제"),
    "ip": ("직접 식별자", "삭제"),
    "address": ("준식별자", "시군구 또는 시도 단위로 범주화"),
    "zip": ("준식별자", "앞 3자리만 유지"),
    "birth_date": ("준식별자", "연령(5세 구간)으로 변환, 90세 이상 상단 코딩"),
    "age": ("준식별자", "5세 구간 범주화"),
    "sex": ("준식별자", "유지"),
    "region": ("준식별자", "시도 단위"),
    "occupation": ("준식별자", "대분류 범주화"),
    "nationality": ("준식별자", "내국인/외국인으로 범주화"),
    "date": ("준식별자", "대상자별 동일 오프셋 날짜 이동 또는 연·월 단위 범주화"),
    "diagnosis": ("민감속성", "유지(희귀질환은 상위 코드로 범주화 검토)"),
    "free_text": ("자유기술", "식별정보 스크러빙 후 검수, 불필요 시 삭제"),
}


def classify_item(item: str) -> tuple[str, str, str]:
    """수집 변수명 → (유형, 분류, 처리 방법)."""
    for dtype, _cat, regex, _act in P.HEADER_RULES:
        if regex.search(item):
            cat, handling = _HANDLING.get(dtype, ("일반 속성", "유지"))
            return P.TYPE_LABELS.get(dtype, dtype), cat, handling
    return "일반 속성", "일반 속성", "유지(이상치는 상·하단 코딩 검토)"


def _context(db: Session, protocol: Protocol) -> dict[str, Any]:
    rs = get_ruleset()
    co = [u for u in (db.get(User, int(uid)) for uid in protocol.co_investigators or []) if u]
    items = [x.strip() for x in protocol.data_items.splitlines() if x.strip()]
    result = wf.preview(db, protocol, kind="initial")
    last_ok = wf._last_approved_snapshot(protocol, exclude=None)
    changes = diff_snapshots(last_ok.snapshot, snapshot_protocol(protocol), rs) if last_ok else []
    return {
        "p": protocol,
        "pi": protocol.pi,
        "co": co,
        "team": [protocol.pi, *co],
        "rs": rs,
        "settings": get_settings(),
        "today": date.today(),
        "items": [(i, *classify_item(i)) for i in items],
        "identifiers": [(rs.label("identifiers", i), _HANDLING.get(i, ("직접 식별자", "삭제"))[1])
                        for i in protocol.identifiers_collected or []],
        "result": result,
        "changes": changes,
        "base_version": last_ok.version if last_ok else None,
    }


def render(db: Session, protocol: Protocol, doc_type: str) -> str:
    from .web.deps import templates

    if doc_type not in GENERATABLE:
        raise ValueError(f"자동 작성할 수 없는 서류입니다: {doc_type}")
    tpl = templates.env.get_template(GENERATABLE[doc_type])
    return tpl.render(**_context(db, protocol), doc_type=doc_type, doc_label=get_ruleset().label("document_types", doc_type))


def generate_and_attach(db: Session, user: User, protocol: Protocol, doc_type: str, ip: str = "") -> Document:
    if not user.is_irb_staff:
        wf.require_member(user, protocol)
    html = render(db, protocol, doc_type).encode("utf-8")
    settings = get_settings()
    folder = settings.upload_dir / str(protocol.id)
    folder.mkdir(parents=True, exist_ok=True)
    label = get_ruleset().label("document_types", doc_type)
    filename = f"{label}_자동작성_v{protocol.version}.html"
    path = folder / f"{uuid.uuid4().hex}_{doc_type}.html"
    path.write_bytes(html)
    version = 1 + max((d.version for d in protocol.documents if d.doc_type == doc_type), default=0)
    doc = Document(
        protocol_id=protocol.id, doc_type=doc_type, filename=filename, stored_path=str(path),
        sha256=hashlib.sha256(html).hexdigest(), size=len(html), version=version, uploaded_by=user.id,
    )
    db.add(doc)
    db.flush()
    audit.record(db, user, "document_generate", "protocol", protocol.id,
                 {"doc_type": doc_type, "doc_id": doc.id, "sha256": doc.sha256}, ip)
    db.refresh(protocol)
    return doc
