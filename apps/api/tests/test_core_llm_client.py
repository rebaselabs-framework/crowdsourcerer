"""Tests for core/llm_client.py — provider-agnostic LLM client."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from core.llm_client import (
    AnthropicProvider,
    GeminiProvider,
    LLMCompletion,
    LLMError,
    LLMUnavailableError,
    OpenAIProvider,
    _auto_detect,
    _strip_system_messages,
    get_llm_client,
    reset_client_cache,
)


def _mock_settings(
    *,
    provider: str = "",
    anthropic_key: str = "",
    gemini_key: str = "",
    openai_key: str = "",
) -> MagicMock:
    s = MagicMock()
    s.llm_provider = provider
    s.llm_default_model = ""
    s.llm_timeout_seconds = 30.0
    s.anthropic_api_key = anthropic_key
    s.anthropic_base_url = "https://api.anthropic.com"
    s.anthropic_default_model = "claude-haiku-4-5-20251001"
    s.gemini_api_key = gemini_key
    s.gemini_base_url = "https://generativelanguage.googleapis.com"
    s.gemini_default_model = "gemini-2.5-flash"
    s.openai_api_key = openai_key
    s.openai_base_url = "https://api.openai.com"
    s.openai_default_model = "gpt-4o-mini"
    return s


class _FakeAsyncClient:
    """httpx.AsyncClient stand-in used via patch."""

    def __init__(self, response: httpx.Response, *, on_post=None):
        self._response = response
        self._on_post = on_post

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, *, json, headers):
        if self._on_post is not None:
            self._on_post(url, json, headers)
        return self._response


# ── _strip_system_messages ────────────────────────────────────────────


class TestStripSystemMessages:
    def test_extracts_system_to_top_level(self):
        cleaned, system = _strip_system_messages([
            {"role": "system", "content": "be kind"},
            {"role": "user", "content": "hi"},
        ])
        assert cleaned == [{"role": "user", "content": "hi"}]
        assert system == "be kind"

    def test_multiple_system_messages_joined(self):
        _, system = _strip_system_messages([
            {"role": "system", "content": "rule 1"},
            {"role": "system", "content": "rule 2"},
            {"role": "user", "content": "ok"},
        ])
        assert system == "rule 1\n\nrule 2"

    def test_drops_unknown_roles(self):
        cleaned, _ = _strip_system_messages([
            {"role": "user", "content": "a"},
            {"role": "tool", "content": "nope"},
            {"role": "assistant", "content": "b"},
        ])
        assert cleaned == [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]

    def test_no_system_messages_returns_none(self):
        _, system = _strip_system_messages([{"role": "user", "content": "hi"}])
        assert system is None


# ── Anthropic provider ────────────────────────────────────────────────


class TestAnthropicProvider:
    def test_empty_key_raises(self):
        with pytest.raises(LLMUnavailableError):
            AnthropicProvider(api_key="")

    def test_strips_trailing_slash_in_base_url(self):
        p = AnthropicProvider(api_key="sk", base_url="https://api.anthropic.com/")
        assert p.base_url == "https://api.anthropic.com"

    @pytest.mark.asyncio
    async def test_success_returns_completion(self):
        response = httpx.Response(
            status_code=200,
            json={
                "id": "msg_123",
                "model": "claude-haiku-4-5-20251001",
                "content": [{"type": "text", "text": "hello there"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
            request=httpx.Request("POST", "http://test"),
        )
        with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(response)):
            result = await AnthropicProvider(api_key="sk").complete(
                messages=[{"role": "user", "content": "hi"}],
            )
        assert isinstance(result, LLMCompletion)
        assert result.provider == "anthropic"
        assert result.text == "hello there"
        assert result.input_tokens == 10
        assert result.output_tokens == 5

    @pytest.mark.asyncio
    async def test_system_prompt_goes_to_top_level(self):
        captured: dict = {}
        response = httpx.Response(
            status_code=200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {},
            },
            request=httpx.Request("POST", "http://test"),
        )

        def _capture(url, json, headers):
            captured["url"] = url
            captured["json"] = json

        with patch(
            "httpx.AsyncClient",
            return_value=_FakeAsyncClient(response, on_post=_capture),
        ):
            await AnthropicProvider(api_key="sk").complete(
                messages=[{"role": "user", "content": "hi"}],
                system="you are helpful",
            )

        assert captured["url"].endswith("/v1/messages")
        assert captured["json"]["system"] == "you are helpful"
        assert all(m.get("role") != "system" for m in captured["json"]["messages"])

    @pytest.mark.asyncio
    async def test_http_error_raises_llm_error(self):
        response = httpx.Response(
            status_code=500,
            json={"error": "boom"},
            request=httpx.Request("POST", "http://test"),
        )
        with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(response)):
            with pytest.raises(LLMError):
                await AnthropicProvider(api_key="sk").complete(
                    messages=[{"role": "user", "content": "hi"}]
                )


# ── Gemini provider ───────────────────────────────────────────────────


class TestGeminiProvider:
    def test_empty_key_raises(self):
        with pytest.raises(LLMUnavailableError):
            GeminiProvider(api_key="")

    @pytest.mark.asyncio
    async def test_success_returns_completion(self):
        response = httpx.Response(
            status_code=200,
            json={
                "modelVersion": "gemini-2.5-flash",
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": "gemini says hi"}],
                            "role": "model",
                        },
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 12,
                    "candidatesTokenCount": 7,
                },
            },
            request=httpx.Request("POST", "http://test"),
        )
        with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(response)):
            result = await GeminiProvider(api_key="sk").complete(
                messages=[{"role": "user", "content": "hi"}],
            )
        assert result.provider == "gemini"
        assert result.text == "gemini says hi"
        assert result.input_tokens == 12
        assert result.output_tokens == 7
        assert result.stop_reason == "STOP"

    @pytest.mark.asyncio
    async def test_system_instruction_sent_at_top_level(self):
        captured: dict = {}
        response = httpx.Response(
            status_code=200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "ok"}]}}
                ],
                "usageMetadata": {},
            },
            request=httpx.Request("POST", "http://test"),
        )

        def _capture(url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers

        with patch(
            "httpx.AsyncClient",
            return_value=_FakeAsyncClient(response, on_post=_capture),
        ):
            await GeminiProvider(api_key="sk").complete(
                messages=[{"role": "user", "content": "hi"}],
                system="you are helpful",
            )

        assert ":generateContent" in captured["url"]
        assert "gemini-2.5-flash" in captured["url"]
        assert captured["json"]["systemInstruction"] == {
            "parts": [{"text": "you are helpful"}]
        }
        assert captured["headers"]["x-goog-api-key"] == "sk"

    @pytest.mark.asyncio
    async def test_assistant_role_maps_to_model(self):
        captured: dict = {}
        response = httpx.Response(
            status_code=200,
            json={
                "candidates": [{"content": {"parts": [{"text": "x"}]}}],
                "usageMetadata": {},
            },
            request=httpx.Request("POST", "http://test"),
        )

        def _capture(url, json, headers):
            captured["json"] = json

        with patch(
            "httpx.AsyncClient",
            return_value=_FakeAsyncClient(response, on_post=_capture),
        ):
            await GeminiProvider(api_key="sk").complete(
                messages=[
                    {"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"},
                    {"role": "user", "content": "follow up"},
                ],
            )

        roles = [c["role"] for c in captured["json"]["contents"]]
        assert roles == ["user", "model", "user"]


# ── OpenAI provider ───────────────────────────────────────────────────


class TestOpenAIProvider:
    def test_empty_key_raises(self):
        with pytest.raises(LLMUnavailableError):
            OpenAIProvider(api_key="")

    @pytest.mark.asyncio
    async def test_success_returns_completion(self):
        response = httpx.Response(
            status_code=200,
            json={
                "id": "chatcmpl-123",
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "openai response"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 8,
                    "total_tokens": 28,
                },
            },
            request=httpx.Request("POST", "http://test"),
        )
        with patch("httpx.AsyncClient", return_value=_FakeAsyncClient(response)):
            result = await OpenAIProvider(api_key="sk").complete(
                messages=[{"role": "user", "content": "hi"}],
            )
        assert result.provider == "openai"
        assert result.text == "openai response"
        assert result.input_tokens == 20
        assert result.output_tokens == 8

    @pytest.mark.asyncio
    async def test_system_prompt_prepended_as_message(self):
        captured: dict = {}
        response = httpx.Response(
            status_code=200,
            json={
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {},
            },
            request=httpx.Request("POST", "http://test"),
        )

        def _capture(url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers

        with patch(
            "httpx.AsyncClient",
            return_value=_FakeAsyncClient(response, on_post=_capture),
        ):
            await OpenAIProvider(api_key="sk").complete(
                messages=[{"role": "user", "content": "hi"}],
                system="you are helpful",
            )

        assert captured["url"].endswith("/v1/chat/completions")
        assert captured["headers"]["Authorization"] == "Bearer sk"
        msgs = captured["json"]["messages"]
        assert msgs[0] == {"role": "system", "content": "you are helpful"}
        assert msgs[1]["role"] == "user"


# ── Factory + auto-detect ─────────────────────────────────────────────


class TestAutoDetect:
    def test_single_key_wins(self):
        assert _auto_detect(_mock_settings(anthropic_key="sk")) == "anthropic"
        assert _auto_detect(_mock_settings(gemini_key="sk")) == "gemini"
        assert _auto_detect(_mock_settings(openai_key="sk")) == "openai"

    def test_no_keys_raises(self):
        with pytest.raises(LLMUnavailableError):
            _auto_detect(_mock_settings())

    def test_multiple_keys_prefer_anthropic(self):
        chosen = _auto_detect(
            _mock_settings(anthropic_key="a", gemini_key="b", openai_key="c")
        )
        assert chosen == "anthropic"

    def test_gemini_and_openai_prefer_gemini(self):
        chosen = _auto_detect(_mock_settings(gemini_key="a", openai_key="b"))
        assert chosen == "gemini"


class TestGetLLMClient:
    def setup_method(self):
        reset_client_cache()

    def teardown_method(self):
        reset_client_cache()

    def test_explicit_provider_honored(self):
        client = get_llm_client(
            _mock_settings(provider="gemini", gemini_key="sk")
        )
        assert isinstance(client, GeminiProvider)

    def test_auto_detect_when_provider_empty(self):
        client = get_llm_client(_mock_settings(openai_key="sk"))
        assert isinstance(client, OpenAIProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(LLMUnavailableError):
            get_llm_client(_mock_settings(provider="vertex", gemini_key="sk"))

    def test_empty_config_raises(self):
        with pytest.raises(LLMUnavailableError):
            get_llm_client(_mock_settings())

    def test_cache_returns_same_instance(self):
        s = _mock_settings(anthropic_key="sk")
        first = get_llm_client(s)
        second = get_llm_client(s)
        assert first is second

    def test_cache_rebuilds_on_key_change(self):
        first = get_llm_client(_mock_settings(anthropic_key="sk-1"))
        second = get_llm_client(_mock_settings(anthropic_key="sk-2"))
        assert first is not second
