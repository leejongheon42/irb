"""Claude API 클라이언트 래퍼.

모든 AI 기능(사전검토 보조, 문서 OCR, 정보 추출, 분류, 요약, 지식 질의응답)이 이 모듈만 거쳐 Claude를 호출한다.

- 모델: settings.ai_model (기본 claude-opus-5-5), thinking 파라미터는 생략(적응형), effort는 호출마다 명시
- 서버측 폴백: betas=["server-side-fallback-2026-07-01"] + fallbacks="default" (안전 분류기 거절 시 권장 모델로 재시도)
- 구조화 출력: output_config.format = {"type": "json_schema", "schema": ...}
- stop_reason 이 refusal / max_tokens 이면 AIError
- SDK 예외는 구체 타입부터 처리해 AIError / AIUnavailable 로 정규화
- 큰 max_tokens(>16000)는 스트리밍(get_final_message)으로 HTTP 타임아웃을 피한다

AI가 비활성화되어 있거나 SDK·자격증명이 없으면 AIUnavailable 을 던진다.
호출하는 쪽은 이를 잡아 규칙 기반 경로로 계속 진행해야 한다.
"""
from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from ..config import get_settings

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
STREAM_THRESHOLD_TOKENS = 16000
MAX_BINARY_BYTES = 30 * 1024 * 1024  # 요청 한도 32MB 이하로 여유를 둔다
IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


class AIError(Exception):
    """API 오류, 거절, 길이 초과, 응답 형식 오류."""


class AIUnavailable(AIError):
    """AI 비활성, SDK 미설치, 자격증명 없음."""


# ------------------------------------------------------------------ 클라이언트 생성(테스트에서 교체 가능)
_client_factory: Callable[[], Any] | None = None
_client_lock = threading.Lock()
_client_cache: Any = None


def set_client_factory(factory: Callable[[], Any] | None) -> None:
    """테스트나 사내 프록시용 클라이언트 주입. None 이면 기본 anthropic.Anthropic()."""
    global _client_factory, _client_cache
    with _client_lock:
        _client_factory = factory
        _client_cache = None


def _sdk():
    try:
        import anthropic
    except ImportError as e:
        raise AIUnavailable("anthropic 패키지가 설치되어 있지 않습니다 (pip install -r requirements-ai.txt).") from e
    return anthropic


def _client() -> Any:
    global _client_cache
    with _client_lock:
        if _client_cache is not None:
            return _client_cache
        try:
            if _client_factory is not None:
                _client_cache = _client_factory()
            else:
                _client_cache = _sdk().Anthropic()
        except AIUnavailable:
            raise
        except Exception as e:  # SDK 생성 단계의 설정 오류(자격증명 형식 등)
            raise AIUnavailable(f"Claude 클라이언트를 만들 수 없습니다: {e}") from e
        return _client_cache


def ai_enabled() -> bool:
    """AI 기능 사용 가능 여부(설정 + SDK 설치 또는 주입된 클라이언트)."""
    if not get_settings().ai_enabled:
        return False
    if _client_factory is not None:
        return True
    try:
        _sdk()
    except AIUnavailable:
        return False
    return True


# ------------------------------------------------------------------ 사용 기록(관리 화면 표시용, 내용은 저장하지 않음)
_usage: deque[dict[str, Any]] = deque(maxlen=200)


def recent_usage() -> list[dict[str, Any]]:
    return list(_usage)


def _record_usage(purpose: str, model: str, ok: bool, elapsed: float, response: Any = None, error: str = "") -> None:
    usage = getattr(response, "usage", None)
    _usage.appendleft({
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "purpose": purpose or "-",
        "model": getattr(response, "model", None) or model,
        "ok": ok,
        "elapsed_s": round(elapsed, 2),
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "error": error,
    })


# ------------------------------------------------------------------ 호출
def create_message(
    *,
    system: str,
    messages: list[dict],
    max_tokens: int = 16000,
    effort: str = "medium",
    output_schema: dict | None = None,
    purpose: str = "",
):
    """Claude Messages API 호출. BetaMessage 를 반환한다."""
    if not get_settings().ai_enabled:
        raise AIUnavailable("AI 기능이 비활성화되어 있습니다(IRB_AI_ENABLED=true 로 활성화, 기관 승인 필요).")
    try:
        import anthropic
    except ImportError:
        anthropic = None
        if _client_factory is None:
            raise AIUnavailable("anthropic 패키지가 설치되어 있지 않습니다 (pip install -r requirements-ai.txt).") from None
    client = _client()
    model = get_settings().ai_model
    output_config: dict[str, Any] = {"effort": effort}
    if output_schema is not None:
        output_config["format"] = {"type": "json_schema", "schema": output_schema}
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
        "output_config": output_config,
    }
    started = time.monotonic()
    try:
        if max_tokens > STREAM_THRESHOLD_TOKENS:
            with client.beta.messages.stream(**kwargs) as stream:
                response = stream.get_final_message()
        else:
            response = client.beta.messages.create(**kwargs)
    except Exception as e:
        err = _normalize_exception(e, anthropic)
        _record_usage(purpose, model, False, time.monotonic() - started, error=str(err))
        raise err from e

    stop = getattr(response, "stop_reason", None)
    if stop == "refusal":
        _record_usage(purpose, model, False, time.monotonic() - started, response, "refusal")
        raise AIError("AI가 이 요청에 응답하지 않았습니다(안전 정책). 규칙 기반 결과를 참고하십시오.")
    if stop == "max_tokens":
        _record_usage(purpose, model, False, time.monotonic() - started, response, "max_tokens")
        raise AIError("AI 응답이 길이 제한으로 잘렸습니다.")
    _record_usage(purpose, model, True, time.monotonic() - started, response)
    return response


def _normalize_exception(e: Exception, anthropic: Any) -> AIError:
    """SDK 예외를 구체 타입부터 AIError / AIUnavailable 로 변환한다."""
    if isinstance(e, AIError):
        return e
    if anthropic is not None:
        if isinstance(e, anthropic.AuthenticationError):
            return AIUnavailable("Claude API 인증 실패 - ANTHROPIC_API_KEY 등 자격증명을 확인하십시오.")
        if isinstance(e, anthropic.PermissionDeniedError):
            return AIError("Claude API 권한 없음 - 조직·워크스페이스 설정을 확인하십시오.")
        if isinstance(e, anthropic.NotFoundError):
            return AIError(f"모델 또는 엔드포인트를 찾을 수 없습니다({get_settings().ai_model}).")
        if isinstance(e, anthropic.RateLimitError):
            return AIError("Claude API 요청 한도 초과 - 잠시 후 다시 시도하십시오.")
        if isinstance(e, anthropic.BadRequestError):
            return AIError(f"Claude API 요청 오류: {getattr(e, 'message', e)}")
        if isinstance(e, anthropic.APIStatusError):
            log.warning("Claude API status error %s (request_id=%s)", e.status_code, getattr(e, "request_id", None))
            return AIError(f"Claude API 오류({e.status_code})")
        if isinstance(e, anthropic.APITimeoutError):
            return AIError("Claude API 응답 시간 초과")
        if isinstance(e, anthropic.APIConnectionError):
            return AIError("Claude API 연결 실패 - 네트워크(망분리·프록시)를 확인하십시오.")
    if isinstance(e, TypeError) and "authentication" in str(e).lower():
        return AIUnavailable("Claude API 자격증명이 설정되지 않았습니다(ANTHROPIC_API_KEY).")
    log.exception("unexpected AI client error")
    return AIError(f"AI 호출 실패: {type(e).__name__}")


def response_text(response: Any) -> str:
    return "".join(getattr(b, "text", "") for b in getattr(response, "content", []) if getattr(b, "type", "") == "text")


def _content(content: str | list[dict]) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content


def structured_call_with_response(
    *, system: str, content: str | list[dict], schema: dict, effort: str = "high", max_tokens: int = 16000,
    purpose: str = "",
) -> tuple[dict, Any]:
    response = create_message(
        system=system, messages=[{"role": "user", "content": _content(content)}], max_tokens=max_tokens,
        effort=effort, output_schema=schema, purpose=purpose,
    )
    text = response_text(response)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise AIError("AI 응답 형식 오류(JSON 아님)") from e
    if not isinstance(data, dict):
        raise AIError("AI 응답 형식 오류(객체 아님)")
    return data, response


def structured_call(
    *, system: str, content: str | list[dict], schema: dict, effort: str = "high", max_tokens: int = 16000,
    purpose: str = "",
) -> dict:
    """JSON 스키마로 제약된 응답을 dict 로 반환."""
    data, _ = structured_call_with_response(
        system=system, content=content, schema=schema, effort=effort, max_tokens=max_tokens, purpose=purpose
    )
    return data


def text_call(
    *, system: str, content: str | list[dict], effort: str = "medium", max_tokens: int = 16000, purpose: str = ""
) -> str:
    response = create_message(
        system=system, messages=[{"role": "user", "content": _content(content)}], max_tokens=max_tokens,
        effort=effort, purpose=purpose,
    )
    return response_text(response)


# ------------------------------------------------------------------ 콘텐츠 블록
def _b64(raw: bytes) -> str:
    if len(raw) > MAX_BINARY_BYTES:
        raise AIError(f"파일이 너무 큽니다({len(raw) // (1024 * 1024)}MB) - AI 전송 한도를 초과합니다.")
    return base64.standard_b64encode(raw).decode("ascii")  # 줄바꿈 없는 base64


def pdf_block(raw: bytes) -> dict:
    return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": _b64(raw)}}


def image_block(raw: bytes, media_type: str) -> dict:
    if media_type not in IMAGE_MEDIA_TYPES:
        raise AIError(f"지원하지 않는 이미지 형식입니다: {media_type}")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": _b64(raw)}}


def text_document_block(text: str, title: str, *, citations: bool = False) -> dict:
    """일반 텍스트 문서 블록. citations=True 는 구조화 출력(output_config.format)과 함께 쓸 수 없다."""
    block: dict[str, Any] = {
        "type": "document",
        "source": {"type": "text", "media_type": "text/plain", "data": text},
        "title": title[:200] or "문서",
    }
    if citations:
        block["citations"] = {"enabled": True}
    return block
