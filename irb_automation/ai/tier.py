"""계정별 AI 등급: 누가 요청했는지에 따라 공급자와 모델을 고른다.

  claude : 관리자 계정 → Anthropic API의 유료 Claude 모델(settings.ai_admin_model)
  free   : 방문자를 포함한 그 밖의 계정 → OpenRouter 무료 모델
  (없음) : 로그인과 무관한 실행(CLI·스케줄러·테스트) → 환경 설정(IRB_AI_PROVIDER, IRB_AI_MODEL) 그대로

등급은 로그인할 때 세션에 기록하고, 요청마다 미들웨어가 컨텍스트 변수로 옮긴다.
백그라운드 스레드로 넘길 때는 carry()로 감싸야 등급이 따라간다.
"""
from __future__ import annotations

import contextvars
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from ..config import get_settings

CLAUDE, FREE = "claude", "free"
_tier: contextvars.ContextVar[str | None] = contextvars.ContextVar("irb_ai_tier", default=None)


def tier_for(user: Any) -> str:
    return CLAUDE if getattr(user, "is_admin", False) else FREE


def current() -> str | None:
    return _tier.get()


def set_current(tier: str | None) -> contextvars.Token:
    return _tier.set(tier if tier in (CLAUDE, FREE) else None)


def reset(token: contextvars.Token) -> None:
    _tier.reset(token)


@contextmanager
def use(tier: str | None) -> Iterator[None]:
    token = set_current(tier)
    try:
        yield
    finally:
        _tier.reset(token)


def carry(fn: Callable[..., Any]) -> Callable[..., Any]:
    """지금의 등급을 다른 스레드에서 실행될 함수에 묶는다."""
    tier = current()

    def bound(*args: Any, **kwargs: Any) -> Any:
        with use(tier):
            return fn(*args, **kwargs)

    return bound


def provider() -> str:
    tier = current()
    if tier == CLAUDE:
        return "anthropic"
    if tier == FREE:
        return "openrouter"
    return get_settings().ai_provider


def model() -> str:
    settings = get_settings()
    return settings.ai_admin_model if current() == CLAUDE else settings.ai_model


def label() -> str:
    """화면 표시용."""
    return "무료 모델(OpenRouter)" if provider() == "openrouter" else f"Claude({model()})"
