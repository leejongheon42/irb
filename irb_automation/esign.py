"""전자서명.

서명 시점의 대상 내용(예: 제출 스냅샷, 결정 내용)을 정규화 JSON으로 직렬화해 SHA-256 해시를 남긴다.
서명 후 내용이 바뀌었는지 언제든 검증할 수 있으며, 서명 행위 자체도 감사 로그에 기록된다.
본인 확인은 서명 직전 비밀번호 재입력으로 한다.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit
from .models import Signature, User
from .security import verify_password
from .timeutil import now


class SignatureError(Exception):
    pass


def content_hash(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def sign(
    db: Session,
    user: User,
    password: str,
    meaning: str,
    entity_type: str,
    entity_id: int,
    payload: Any,
    ip: str = "",
) -> Signature:
    if not verify_password(password, user.password_hash):
        raise SignatureError("전자서명을 위한 비밀번호가 올바르지 않습니다.")
    return record_signature(db, user, meaning, entity_type, entity_id, payload, ip)


def record_signature(
    db: Session, user: User, meaning: str, entity_type: str, entity_id: int, payload: Any, ip: str = ""
) -> Signature:
    sig = Signature(
        user_id=user.id,
        meaning=meaning,
        entity_type=entity_type,
        entity_id=entity_id,
        content_hash=content_hash(payload),
        signed_at=now(),
        ip_address=ip,
    )
    db.add(sig)
    db.flush()
    audit.record(db, user, "esign", entity_type, entity_id, {"meaning": meaning, "hash": sig.content_hash}, ip)
    return sig


def verify(sig: Signature, payload: Any) -> bool:
    return sig.content_hash == content_hash(payload)


def signatures_for(db: Session, entity_type: str, entity_id: int) -> list[Signature]:
    return list(
        db.scalars(
            select(Signature)
            .where(Signature.entity_type == entity_type, Signature.entity_id == entity_id)
            .order_by(Signature.id)
        )
    )
