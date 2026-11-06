"""문서 버전 관리.

같은 과제·같은 서류 종류에 새 문서가 들어오면 기존 현행본은 '이전 버전(superseded)'이 되고
새 문서가 현행본이 된다. 업로드·자동 작성·자동 변환·승인 도장본 모두 이 규칙을 따른다.
"""
from __future__ import annotations

from collections import OrderedDict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Document


def register_new_version(db: Session, document: Document) -> list[Document]:
    """document를 해당 서류 종류의 현행본으로 등록하고, 대체된 이전 현행본 목록을 반환한다."""
    if document.id is None:
        db.add(document)
        db.flush()
    others = list(db.scalars(
        select(Document).where(
            Document.protocol_id == document.protocol_id,
            Document.doc_type == document.doc_type,
            Document.id != document.id,
        )
    ))
    top = max((d.version or 1 for d in others), default=0)
    if (document.version or 0) <= top:
        document.version = top + 1
    superseded = []
    for d in others:
        if d.is_current:
            superseded.append(d)
        d.is_current = False
        d.status = "superseded"
    document.is_current = True
    if document.status in (None, "", "superseded"):
        document.status = "submitted"
    db.flush()
    return superseded


def latest_by_type(documents: list[Document]) -> "OrderedDict[str, Document]":
    """서류 종류별 최신(버전·id 최대) 문서. 이전 데이터처럼 플래그가 어긋나 있어도 안전하다."""
    latest: dict[str, Document] = {}
    for d in documents:
        cur = latest.get(d.doc_type)
        if cur is None or ((d.version or 0), d.id or 0) > ((cur.version or 0), cur.id or 0):
            latest[d.doc_type] = d
    return OrderedDict(sorted(latest.items()))


def current_documents(documents: list[Document]) -> list[Document]:
    return list(latest_by_type(documents).values())


def versions_of(documents: list[Document], doc_type: str) -> list[Document]:
    return sorted((d for d in documents if d.doc_type == doc_type), key=lambda d: ((d.version or 0), d.id or 0),
                  reverse=True)
