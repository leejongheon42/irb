"""OpenRouter Chat Completions adapter for the shared AI client."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Final, TypedDict

import yaml
from pydantic import BaseModel, ConfigDict, PositiveInt, TypeAdapter, ValidationError

from ..config import get_settings
from .client import AIError, AIUnavailable

FREE_ROUTER: Final = "openrouter/free"
URL: Final = "https://openrouter.ai/api/v1/chat/completions"
PURPOSE_KEYS: Final = {
    "사전검토 보조": "prereview",
    "서류 품질 점검": "qc",
    "연구계획서 초안 작성": "draft",
    "보완 답변 초안": "revision",
    "문서 OCR": "ocr",
    "서류 분류": "classify",
    "계획서 정보 추출": "extract",
    "문서 검토 요약": "summary",
    "knowledge_answer": "knowledge",
}


class ModelRoute(BaseModel):
    model_config = ConfigDict(frozen=True)
    models: tuple[str, ...]
    max_tokens: PositiveInt


@lru_cache(maxsize=1)
def model_routes() -> dict[str, ModelRoute]:
    with Path(__file__).with_name("openrouter_models.yaml").open(encoding="utf-8") as source:
        routes = TypeAdapter(dict[str, ModelRoute]).validate_python(yaml.safe_load(source))
    if routes.keys() != set(PURPOSE_KEYS.values()) or any(
        not 1 <= len(route.models) <= 2 or any(not model.endswith(":free") for model in route.models)
        for route in routes.values()
    ):
        raise AIUnavailable("OpenRouter에는 기능별 무료 모델을 1~2개 지정하십시오.")
    return routes


def route_for(purpose: str) -> ModelRoute:
    key = PURPOSE_KEYS.get(purpose, purpose)
    route = model_routes().get(key)
    if route is None:
        return ModelRoute(models=(FREE_ROUTER,), max_tokens=8000)
    return ModelRoute(models=(*route.models, FREE_ROUTER), max_tokens=route.max_tokens)


class _TextPart(TypedDict):
    type: str
    text: str


class _ImagePart(TypedDict):
    type: str
    image_url: dict[str, str]


class _Message(TypedDict):
    role: str
    content: str | list[_TextPart | _ImagePart]


def _messages(system: str, messages: list[dict]) -> list[_Message]:
    result: list[_Message] = [{"role": "system", "content": system}] if system else []
    document_number = 0
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            result.append({"role": message["role"], "content": content})
            continue
        parts: list[_TextPart | _ImagePart] = []
        for block in content:
            match block:
                case {"type": "text", "text": str(text)}:
                    parts.append({"type": "text", "text": text})
                case {"type": "image", "source": {"type": "base64", "media_type": str(mime), "data": str(data)}}:
                    parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
                case {"type": "document", "source": {"type": "text", "data": str(text)}}:
                    document_number += 1
                    title = block.get("title") or "문서"
                    parts.append({"type": "text", "text": f"[문서 {document_number}] {title}\n{text}"})
                case _:
                    raise AIError("OpenRouter에서 지원하지 않는 문서 블록입니다.")
        result.append({"role": message["role"], "content": parts})
    return result


class TextBlock(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str
    type: str = "text"


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)
    input_tokens: int | None = None
    output_tokens: int | None = None


class Message(BaseModel):
    model_config = ConfigDict(frozen=True)
    model: str
    content: tuple[TextBlock, ...]
    stop_reason: str | None
    usage: Usage


class _CompletionMessage(BaseModel):
    model_config = ConfigDict(frozen=True)
    content: str | None = None
    refusal: str | None = None


class _APIError(BaseModel):
    model_config = ConfigDict(frozen=True)
    code: int | str | None = None


class _Choice(BaseModel):
    model_config = ConfigDict(frozen=True)
    message: _CompletionMessage
    finish_reason: str | None = None
    error: _APIError | None = None


class _Usage(BaseModel):
    model_config = ConfigDict(frozen=True)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class _Completion(BaseModel):
    model_config = ConfigDict(frozen=True)
    model: str
    choices: list[_Choice]
    usage: _Usage | None = None


class _ErrorEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)
    error: _APIError | None = None


def _status_error(code: int | str | None) -> AIError:
    if code in (401, "401"):
        return AIUnavailable("AI API 인증에 실패했습니다. OPENROUTER_API_KEY를 확인하십시오.")
    if code in (402, "402"):
        return AIError("AI API 요청이 계정의 크레딧 또는 지출 한도로 거절되었습니다.")
    if code in (404, "404"):
        return AIError("AI API에 사용 가능한 무료 모델 엔드포인트가 없습니다. 모델 가용성과 계정 설정을 확인하십시오.")
    if code in (429, "429"):
        return AIError("AI API 요청 한도를 초과했습니다. 잠시 후 다시 시도하십시오.")
    return AIError(f"AI API 오류({code if isinstance(code, int) else '응답 본문'})")


def _retry_delay(value: str | None) -> float | None:
    if not value:
        return None
    if value.isascii() and value.isdecimal() and len(value) <= 10:
        delay = float(value)
    else:
        try:
            when = parsedate_to_datetime(value)
        except (ValueError, OverflowError):
            return None
        if when.tzinfo is None:
            return None
        delay = max(0, (when - datetime.now(timezone.utc)).total_seconds())
    return delay if delay <= 60 else None


class _SlowModel(Exception):
    """The current model did not answer within its share of the time budget."""


def _request(httpx, api_key: str, payload: dict, deadline: float) -> bytes:
    try:
        with httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0)) as http:
            for attempt in range(2):
                chunks: list[bytes] = []
                with http.stream("POST", URL, headers={"Authorization": f"Bearer {api_key}"}, json=payload) as response:
                    for chunk in response.iter_bytes():
                        chunks.append(chunk)
                        if time.monotonic() > deadline:
                            raise _SlowModel
                body = b"".join(chunks)
                try:
                    envelope = _ErrorEnvelope.model_validate_json(body)
                except ValidationError:
                    envelope = _ErrorEnvelope()
                code = response.status_code
                if response.is_success and envelope.error is not None:
                    code = envelope.error.code
                if code in (429, "429") and attempt == 0:
                    delay = _retry_delay(response.headers.get("Retry-After"))
                    if delay is not None and time.monotonic() + delay < deadline:
                        time.sleep(delay)
                        continue
                if not response.is_success or envelope.error is not None:
                    raise _status_error(code)
                return body
    except httpx.TimeoutException:
        raise _SlowModel from None
    except httpx.RequestError as e:
        raise AIError("AI API 연결 실패 - 네트워크 설정을 확인하십시오.") from e
    raise AIError("AI API 요청 한도를 초과했습니다. 잠시 후 다시 시도하십시오.")


def create_message(
    *, api_key: str, route: ModelRoute, system: str, messages: list[dict], max_tokens: int,
    output_schema: dict | None, json_object: bool = False,
) -> Message:
    if not api_key:
        raise AIUnavailable("OPENROUTER_API_KEY가 설정되지 않았습니다.")
    try:
        import httpx
    except ImportError as e:
        raise AIUnavailable("httpx 패키지를 설치하십시오 (pip install -r requirements-ai.txt).") from e

    payload = {
        "models": route.models,
        "messages": _messages(system, messages),
        "max_tokens": min(max_tokens, route.max_tokens),
    }
    if output_schema is not None:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "irb_result", "strict": True, "schema": output_schema},
        }
        payload["provider"] = {"require_parameters": True}
    if json_object:
        payload["response_format"] = {"type": "json_object"}
        payload["provider"] = {"require_parameters": True}

    # 무료 모델은 응답을 수 분씩 끌 수 있다. 호출 전체에 기한을 두고, 한 모델이 제 몫의 시간을 넘기면
    # 목록의 다음 모델부터 다시 요청한다(OpenRouter의 models 폴백은 오류에만 반응하고 지연에는 반응하지 않는다).
    started = time.monotonic()
    budget = max(1, get_settings().ai_timeout_seconds)
    body = b""
    for first in range(len(route.models)):
        last = first == len(route.models) - 1
        deadline = started + budget if last else min(started + budget, time.monotonic() + budget / len(route.models))
        if time.monotonic() >= deadline:
            raise AIError("AI API 응답 시간 초과")
        payload["models"] = route.models[first:]
        try:
            body = _request(httpx, api_key, payload, deadline)
            break
        except _SlowModel:
            if last:
                raise AIError("AI API 응답 시간 초과") from None

    try:
        completion = _Completion.model_validate_json(body)
    except ValidationError:
        raise AIError("AI API 응답 형식이 잘못되었습니다.") from None
    if not completion.choices:
        raise AIError("AI API가 응답을 반환하지 않았습니다.")
    choice = completion.choices[0]
    if choice.error is not None or choice.finish_reason == "error":
        raise _status_error(choice.error.code if choice.error else None)
    stop_reason = {"length": "max_tokens", "content_filter": "refusal", "stop": "end_turn"}.get(
        choice.finish_reason, choice.finish_reason
    )
    if choice.message.refusal:
        stop_reason = "refusal"
    text = choice.message.content or ""
    if not text.strip() and stop_reason not in {"refusal", "max_tokens"} and output_schema is None and not json_object:
        raise AIError("AI API가 비어 있는 응답을 반환했습니다.")
    usage = completion.usage
    return Message(
        model=completion.model, content=(TextBlock(text=text),), stop_reason=stop_reason,
        usage=Usage(input_tokens=usage.prompt_tokens if usage else None,
                    output_tokens=usage.completion_tokens if usage else None),
    )
