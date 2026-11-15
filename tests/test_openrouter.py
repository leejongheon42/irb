from __future__ import annotations

import json
from collections.abc import Callable
from functools import partial

import httpx
import pytest

from irb_automation.ai import client as ai
from irb_automation.docproc.text import extract_text
from irb_automation.knowledge import service as knowledge

from .conftest import make_pdf


@pytest.fixture
def openrouter(settings, monkeypatch) -> Callable[[Callable[[httpx.Request], httpx.Response]], None]:
    settings.ai_provider = "openrouter"
    settings.ai_review_enabled = True
    settings.openrouter_api_key = "test-only"
    client = httpx.Client

    def mock(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        monkeypatch.setattr(httpx, "Client", partial(client, transport=httpx.MockTransport(handler)))

    return mock


def _reply(text: str, finish_reason: str = "stop") -> httpx.Response:
    return httpx.Response(200, json={
        "model": "example/fallback:free",
        "choices": [{"message": {"content": text}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 34},
    })


def test_openrouter_adapts_blocks_routes_and_usage(openrouter):
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-only"
        body = json.loads(request.content)
        assert body["models"] == ["nvidia/nemotron-3.5-super-vl:free", "inclusionai/ling-3.2-flash:free", "openrouter/free"]
        assert body["max_tokens"] == 16000
        assert body["messages"] == [
            {"role": "system", "content": "검토"},
            {"role": "user", "content": [
                {"type": "text", "text": "[문서 1] 샘플\n근거"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}},
                {"type": "text", "text": "질문"},
            ]},
        ]
        assert "output_config" not in body and "betas" not in body
        return _reply("응답")

    openrouter(respond)
    response = ai.create_message(
        system="검토", messages=[{"role": "user", "content": [
            ai.text_document_block("근거", "샘플", citations=True), ai.image_block(b"x", "image/png"),
            {"type": "text", "text": "질문"},
        ]}], max_tokens=32000, purpose="서류 품질 점검",
    )
    assert response.stop_reason == "end_turn" and ai.response_text(response) == "응답"
    latest = ai.recent_usage()[0]
    assert (latest["model"], latest["input_tokens"], latest["output_tokens"]) == ("example/fallback:free", 12, 34)


@pytest.mark.parametrize("first", ['not json', '{"summary": 10}'])
def test_openrouter_retries_invalid_structured_output_once(openrouter, first):
    schema = {"type": "object", "properties": {"summary": {"type": "string"}},
              "required": ["summary"], "additionalProperties": False}
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        assert body["provider"] == {"require_parameters": True}
        if len(calls) == 1:
            assert body["response_format"]["json_schema"] == {"name": "irb_result", "strict": True, "schema": schema}
            return _reply(first)
        assert body["response_format"] == {"type": "json_object"}
        assert json.loads(body["messages"][0]["content"].splitlines()[-1]) == schema
        return _reply('```json\n{"summary": "검토 요약"}\n```')

    openrouter(respond)
    assert ai.structured_call(system="검토", content="문서", schema=schema) == {"summary": "검토 요약"}
    assert len(calls) == 2


@pytest.mark.parametrize(("response", "error", "message"), [
    (httpx.Response(401), ai.AIUnavailable, "인증"),
    (httpx.Response(402), ai.AIError, "크레딧"),
    (httpx.Response(404), ai.AIError, "엔드포인트"),
    (httpx.Response(429), ai.AIError, "요청 한도"),
    (httpx.Response(200, json={"error": {"code": 429, "message": "sensitive details"}}), ai.AIError, "요청 한도"),
    (httpx.Response(200, json={"model": "free", "choices": [{"message": {"content": "partial"},
      "finish_reason": "error", "error": {"code": 502}}]}), ai.AIError, "502"),
    (_reply("", "content_filter"), ai.AIError, "응답하지"),
    (_reply("일부만 생성", "length"), ai.AIError, "길이 제한"),
])
def test_openrouter_normalizes_errors(openrouter, response, error, message):
    openrouter(lambda _: response)
    with pytest.raises(error, match=message) as caught:
        ai.text_call(system="s", content="c")
    assert type(caught.value) is error and not ai.recent_usage()[0]["ok"]
    assert "sensitive details" not in str(caught.value)


def test_openrouter_honors_retry_after_once(openrouter, monkeypatch):
    from irb_automation.ai import openrouter as adapter

    delays: list[float] = []
    monkeypatch.setattr(adapter.time, "sleep", delays.append)
    openrouter(lambda _: httpx.Response(429, headers={"Retry-After": "2"}))
    with pytest.raises(ai.AIError, match="요청 한도"):
        ai.text_call(system="s", content="c")
    assert delays == [2.0]


def test_openrouter_pdf_batches_at_page_limit(openrouter, settings):
    batches = []

    def read_batch(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert len(body["models"]) == 3 and body["models"][-1] == "openrouter/free"
        parts = body["messages"][1]["content"]
        assert all(part["image_url"]["url"].startswith("data:image/png;base64,iVBOR") for part in parts[:-1])
        batches.append(len(parts) - 1)
        return _reply(f"배치 {len(batches)}")

    openrouter(read_batch)
    raw = make_pdf(pages=4)
    settings.ai_ocr_max_pages = 3
    skipped = extract_text("scan.pdf", raw, allow_ai_ocr=True)
    assert skipped.method == "pdf" and any("3쪽" in warning for warning in skipped.warnings)
    assert not batches
    settings.ai_ocr_max_pages = 4
    result = extract_text("scan.pdf", raw, allow_ai_ocr=True)
    assert result.method == "ai_ocr" and result.text == "배치 1\n\n배치 2"
    assert batches == [3, 1]


@pytest.mark.parametrize(("text", "mode"), [("시험용 안내입니다 [2] [99] [2]", "ai"), ("번호가 없습니다 [99]", "extractive")])
def test_openrouter_knowledge_citations_resolve_to_search_hits(openrouter, db, text, mode):
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["messages"][1]["content"][1]["text"].startswith("[문서 2]")
        return _reply(text)

    openrouter(respond)
    result = knowledge.answer(db, "동의 면제 요건", record=False)
    assert result["mode"] == mode
    if mode == "ai":
        assert "[99]" not in result["answer"] and result["answer"].count("[1]") == 2
        assert len(result["citations"]) == 1
        assert result["citations"][0]["chunk_id"] == result["hits"][1]["chunk_id"]
        assert result["citations"][0]["cited_text"] == result["hits"][1]["snippet"]


def test_slow_model_is_skipped_for_the_next_one(openrouter):
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        models = json.loads(request.content)["models"]
        seen.append(models)
        if len(seen) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return _reply("응답")

    openrouter(respond)
    assert ai.text_call(system="검토", content="질문", purpose="서류 품질 점검") == "응답"
    assert seen == [["nvidia/nemotron-3.5-super-vl:free", "inclusionai/ling-3.2-flash:free", "openrouter/free"],
                    ["inclusionai/ling-3.2-flash:free", "openrouter/free"]]
