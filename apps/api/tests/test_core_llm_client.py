"""Tests for core/llm_client.py — direct Anthropic API wrapper."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.llm_client import (
    AnthropicClient,
    LLMCompletion,
    LLMError,
    LLMUnavailableError,
    _first_text_block,
    _normalise_messages,
)


def _anthropic_response(text: str = "hello") -> dict:
    return {
        "id": "msg_123",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


class _FakeAsyncClient:
    """Minimal stand-in for ``httpx.AsyncClient`` used in patches."""

    def __init__(self, response: httpx.Response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *args, **kwargs):
        return self._response


class TestNormaliseMessages:
    def test_strips_system_role(self):
        out = _normalise_messages([
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": "hi"},
        ])
        assert out == [{"role": "user", "content": "hi"}]

    def test_drops_unknown_roles(self):
        out = _normalise_messages([
            {"role": "user", "content": "a"},
            {"role": "tool", "content": "nope"},
            {"role": "assistant", "content": "b"},
        ])
        assert out == [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]


class TestFirstTextBlock:
    def test_returns_first_text(self):
        data = _anthropic_response("the answer")
        assert _first_text_block(data) == "the answer"

    def test_skips_non_text_blocks(self):
        data = {"content": [{"type": "tool_use"}, {"type": "text", "text": "ok"}]}
        assert _first_text_block(data) == "ok"

    def test_empty_content_returns_empty(self):
        assert _first_text_block({"content": []}) == ""

    def test_missing_content_returns_empty(self):
        assert _first_text_block({}) == ""


class TestInit:
    def test_empty_key_raises(self):
        with pytest.raises(LLMUnavailableError):
            AnthropicClient(api_key="")

    def test_valid_key_stores_state(self):
        c = AnthropicClient(
            api_key="sk-live-xxx",
            base_url="https://api.anthropic.com/",
            default_model="claude-haiku-4-5",
            timeout=30.0,
        )
        # Trailing slash stripped from base_url.
        assert c._base_url == "https://api.anthropic.com"


class TestComplete:
    @pytest.mark.asyncio
    async def test_success_returns_completion(self):
        response = httpx.Response(
            status_code=200,
            json=_anthropic_response("hello there"),
            request=httpx.Request("POST", "http://test"),
        )
        client = AnthropicClient(api_key="sk")

        with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(response)):
            result = await client.complete(
                messages=[{"role": "user", "content": "hi"}],
            )

        assert isinstance(result, LLMCompletion)
        assert result.text == "hello there"
        assert result.input_tokens == 10
        assert result.output_tokens == 5
        assert result.stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_http_error_raises_llm_error(self):
        response = httpx.Response(
            status_code=500,
            json={"error": {"type": "internal_error", "message": "boom"}},
            request=httpx.Request("POST", "http://test"),
        )
        client = AnthropicClient(api_key="sk")

        with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(response)):
            with pytest.raises(LLMError):
                await client.complete(messages=[{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_network_error_raises_llm_error(self):
        class _FailingClient(_FakeAsyncClient):
            async def post(self, *args, **kwargs):
                raise httpx.ConnectError("refused")

        client = AnthropicClient(api_key="sk")
        with patch(
            "httpx.AsyncClient",
            return_value=_FailingClient(None),  # type: ignore[arg-type]
        ):
            with pytest.raises(LLMError):
                await client.complete(messages=[{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_system_prompt_passed_top_level(self):
        """When ``system`` is set, Anthropic wants it at the top level, not in messages."""
        captured: dict = {}

        class _CapturingClient(_FakeAsyncClient):
            async def post(self, url, *, json, headers):
                captured["json"] = json
                return self._response

        response = httpx.Response(
            status_code=200,
            json=_anthropic_response("ok"),
            request=httpx.Request("POST", "http://test"),
        )
        client = AnthropicClient(api_key="sk")

        with patch("httpx.AsyncClient", return_value=_CapturingClient(response)):
            await client.complete(
                messages=[{"role": "user", "content": "hi"}],
                system="you are helpful",
            )

        assert captured["json"]["system"] == "you are helpful"
        # System message should NOT be in the messages array.
        assert all(m.get("role") != "system" for m in captured["json"]["messages"])
