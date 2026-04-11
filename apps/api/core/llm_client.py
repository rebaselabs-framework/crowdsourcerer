"""Direct Anthropic API client.

Thin httpx-based wrapper around ``POST /v1/messages``. No dependency on
the `anthropic` SDK — keeps requirements.txt light and lets us pin the
timeout + retry policy ourselves.

Callers::

    from core.llm_client import get_llm_client, LLMUnavailableError

    client = get_llm_client()  # reads settings.anthropic_api_key
    text = await client.complete(
        system="You are a helpful assistant.",
        messages=[{"role": "user", "content": "Hello"}],
        max_tokens=1024,
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from core.config import Settings, get_settings

logger = structlog.get_logger()


class LLMError(RuntimeError):
    """Generic LLM-call failure (network, 5xx, bad response shape)."""


class LLMUnavailableError(LLMError):
    """Raised when ``ANTHROPIC_API_KEY`` is not configured."""


@dataclass(frozen=True, slots=True)
class LLMCompletion:
    """Result of a single Anthropic messages call."""

    text: str
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    raw: dict[str, Any]


class AnthropicClient:
    """Stateless direct client for the Anthropic Messages API."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.anthropic.com",
        default_model: str = "claude-haiku-4-5-20251001",
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise LLMUnavailableError(
                "ANTHROPIC_API_KEY is not set — llm_generate / data_transform / "
                "web_research tasks are unavailable."
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._timeout = timeout

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> LLMCompletion:
        """Call ``POST /v1/messages`` and return the first text block."""
        payload: dict[str, Any] = {
            "model": model or self._default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": _normalise_messages(messages),
        }
        if system:
            payload["system"] = system

        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    f"{self._base_url}/v1/messages",
                    json=payload,
                    headers=headers,
                )
            except (httpx.HTTPError, OSError) as exc:
                raise LLMError(f"Anthropic request failed: {exc}") from exc

        if resp.status_code >= 400:
            try:
                detail = resp.json()
            except ValueError:
                detail = resp.text
            raise LLMError(
                f"Anthropic returned HTTP {resp.status_code}: {detail}"
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"Anthropic response was not JSON: {exc}") from exc

        text = _first_text_block(data)
        usage = data.get("usage") or {}
        return LLMCompletion(
            text=text,
            model=data.get("model") or payload["model"],
            stop_reason=data.get("stop_reason"),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            raw=data,
        )


def _normalise_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip any ``role == 'system'`` entries (Anthropic takes those via the
    top-level ``system`` field) and coerce content to Anthropic's expected
    shape."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            continue
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
        elif isinstance(content, list):
            out.append({"role": role, "content": content})
        else:
            out.append({"role": role, "content": str(content or "")})
    return out


def _first_text_block(data: dict[str, Any]) -> str:
    content = data.get("content")
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                return text
    return ""


# ── Module-level singleton ────────────────────────────────────────────

_client: AnthropicClient | None = None
_client_key: tuple[str, str, str] | None = None


def get_llm_client(settings: Settings | None = None) -> AnthropicClient:
    """Return a cached :class:`AnthropicClient` built from Settings.

    Raises :class:`LLMUnavailableError` if ``anthropic_api_key`` is empty.
    Callers should catch that and convert it to an HTTP 503 / WorkerError.
    """
    global _client, _client_key
    s = settings or get_settings()
    key = (s.anthropic_api_key, s.anthropic_base_url, s.anthropic_default_model)
    if _client is None or _client_key != key:
        _client = AnthropicClient(
            api_key=s.anthropic_api_key,
            base_url=s.anthropic_base_url,
            default_model=s.anthropic_default_model,
            timeout=s.anthropic_timeout_seconds,
        )
        _client_key = key
    return _client


__all__ = [
    "AnthropicClient",
    "LLMCompletion",
    "LLMError",
    "LLMUnavailableError",
    "get_llm_client",
]
