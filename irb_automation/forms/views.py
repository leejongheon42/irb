"""템플릿 도우미(Jinja 전역 함수). 라우트를 고치지 않고도 화면에서 기관 양식을 쓰기 위해 짧은 별도 세션을 사용한다."""
from __future__ import annotations

from typing import Any

from ..database import SessionLocal
from ..models import Protocol
from .logic import display
from .online import STATUS_LABELS, form_status
from .packs import ensure_seeded
from .schema import active_protocol_sections


def protocol_sections() -> list[dict[str, Any]]:
    """활성 '과제 신청서 기관 추가 항목' 구역 목록(최초 진입 시 기본 양식 팩 자동 로드)."""
    ensure_seeded()
    with SessionLocal() as db:
        return active_protocol_sections(db)


def online_progress(protocol_id: int) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        p = db.get(Protocol, protocol_id)
        if p is None:
            return []
        return [
            {
                "doc_type": st["spec"].doc_type,
                "label": st["spec"].label,
                "per_member": st["spec"].per_member,
                "mandatory": st["spec"].required,
                "submitted": st["submitted"],
                "required": st["required"],
                "complete": st["complete"],
            }
            for st in form_status(db, p)
        ]


def field_show_when(field: Any, prefix: str) -> str:
    return field.data_show_when(prefix) if hasattr(field, "data_show_when") else ""


def field_display(field: Any, value: Any) -> str:
    return display(field, value)


def online_status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)
