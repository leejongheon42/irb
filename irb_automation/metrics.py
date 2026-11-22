"""Persistent service metrics: one row per LLM call (timing and token counts only, never content).

AI calls made inside a review are buffered and written after the review commits, so the metric
write never competes with the review's own transaction. Calls outside a review write immediately.
"""
from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, SessionLocal
from .timeutil import now

log = logging.getLogger(__name__)


class AICall(Base):
    __tablename__ = "ai_calls"
    __table_args__ = (Index("ix_ai_calls_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    user_id: Mapped[int | None] = mapped_column(Integer, default=None)
    run_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    provider: Mapped[str] = mapped_column(String(20), default="")
    purpose: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(String(120), default="")
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    output_tokens: Mapped[int | None] = mapped_column(Integer, default=None)


@dataclass
class Scope:
    user_id: int | None = None
    run_id: int | None = None
    calls: list[AICall] = field(default_factory=list)


_scope: contextvars.ContextVar[Scope | None] = contextvars.ContextVar("irb_metrics_scope", default=None)


@contextmanager
def review_scope(run_id: int, user_id: int | None = None) -> Iterator[Scope]:
    """Attribute AI calls to a review and write them once the block ends."""
    scope = Scope(user_id=user_id, run_id=run_id)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)
        for call in scope.calls:
            call.user_id = call.user_id or scope.user_id
        _write(scope.calls)


def record_ai_call(*, provider: str, purpose: str, model: str, ok: bool, elapsed_s: float,
                   input_tokens: int | None = None, output_tokens: int | None = None, error: str = "") -> None:
    call = AICall(created_at=now(), provider=provider[:20], purpose=(purpose or "-")[:60], model=(model or "")[:120],
                  ok=ok, error=_error_kind(error), elapsed_ms=max(0, int(elapsed_s * 1000)),
                  input_tokens=input_tokens if isinstance(input_tokens, int) else None,
                  output_tokens=output_tokens if isinstance(output_tokens, int) else None)
    scope = _scope.get()
    if scope is not None:
        call.run_id = scope.run_id
        scope.calls.append(call)
        return
    _write([call])


def _error_kind(error: str) -> str:
    """Keep a short, content-free error label for grouping."""
    if not error:
        return ""
    lowered = error.lower()
    for key, label in (("refusal", "거절"), ("max_tokens", "길이 초과"), ("시간 초과", "시간 초과"),
                       ("timeout", "시간 초과"), ("한도", "요청 한도"), ("rate", "요청 한도"),
                       ("인증", "인증 실패"), ("형식", "응답 형식"), ("비활성", "AI 비활성")):
        if key in lowered:
            return label
    return "기타 오류"


def _write(calls: list[AICall]) -> None:
    if not calls:
        return
    try:
        with SessionLocal() as db:
            db.add_all(calls)
            db.commit()
    except Exception:  # Metrics are best effort and must never break an AI feature.
        log.warning("Failed to store AI call metrics", exc_info=True)
