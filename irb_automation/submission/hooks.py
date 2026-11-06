"""문서 저장 후 후속 처리 연결점.

업로드 직후 문서 인식·변환·검색 색인 파이프라인(pipeline.service, 선택 모듈)을 시작한다.
파이프라인이 없거나 실패해도 업로드는 그대로 유지된다(SAVEPOINT로 격리).
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from ..models import Document, User

log = logging.getLogger(__name__)


def after_document_saved(db: Session, user: User, document: Document) -> None:
    try:
        from ..pipeline.service import start_document_pipeline
    except ImportError:
        return
    try:
        with db.begin_nested():
            start_document_pipeline(db, user, document.id)
    except Exception:  # 후속 처리 실패가 제출을 막지 않도록 한다
        log.exception("document pipeline failed for document %s", document.id)


def finalize_system_document(db: Session, user: User, document: Document, source: str,
                             versioned: bool = True) -> None:
    """자동 작성·웹 서식 등 시스템이 만든 문서를 제출 체계(버전·후속 처리)에 등록한다.

    versioned=False는 연구진 개인별 서식처럼 같은 종류 문서가 여러 건 동시에 유효한 경우에 쓴다.
    """
    document.source = source
    if versioned:
        from .versions import register_new_version

        register_new_version(db, document)
    after_document_saved(db, user, document)
