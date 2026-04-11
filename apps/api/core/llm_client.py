"""Provider-agnostic LLM client.

Supports three backends behind one interface:

- **Anthropic**  — ``POST /v1/messages`` (header ``x-api-key``)
- **Gemini**     — ``POST /v1beta/models/{model}:generateContent``
- **OpenAI**     — ``POST /v1/chat/completions`` (header ``Authorization: Bearer``)

Callers never care which one is wired up::

    from core.llm_client import get_llm_client

    client = get_llm_client()  # reads settings — picks provider for you
    completion = await client.complete(
        messages=[{"role": "user", "content": "hi"}],
        system="You are helpful.",
        max_tokens=1024,
    )
    print(completion.text)

Provider selection
------------------

The factory reads ``settings.llm_provider``. When that's empty it
auto-detects: if exactly one of ``anthropic_api_key`` /
``gemini_api_key`` / ``openai_api_key`` is set, that provider wins.
Multiple keys with no explicit preference → prefer Anthropic (it was
the original default) and warn. Zero keys → :class:`LLMUnavailableError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
import structlog

from core.config import Settings, get_settings

logger = structlog.get_logger()

ProviderName = Literal["anthropic", "gemini", "openai"]


class LLMError(RuntimeError):
    """Generic LLM-call failure (network, 5xx, bad response shape)."""


class LLMUnavailableError(LLMError):
    """Raised when no LLM provider is configured."""


@dataclass(frozen=True, slots=True)
class LLMCompletion:
    """Result of a single LLM call. Shape is provider-agnostic."""

    text: str
    model: str
    provider: ProviderName
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    raw: dict[str, Any]


class LLMProvider(Protocol):
    """Structural interface every provider adapter implements."""

    name: ProviderName
    base_url: str
    default_model: str

    async def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str | None = None,
        model: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> LLMCompletion: ...


# ── Shared helpers ────────────────────────────────────────────────────


def _strip_system_messages(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Split out any ``role: system`` entries embedded in the messages list.

    Anthropic + Gemini want the system instruction at the top level;
    OpenAI is fine either way. Returning both lets each provider pick.
    """
    system_parts: list[str] = []
    cleaned: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            content = msg.get("content")
            if isinstance(content, str):
                system_parts.append(content)
            continue
        if role not in ("user", "assistant"):
            continue
        cleaned.append(msg)
    return cleaned, ("\n\n".join(system_parts) if system_parts else None)


async def _post_json(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    provider: ProviderName,
) -> dict[str, Any]:
    """Single POST + JSON-decode, converting failures to LLMError."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
        except (httpx.HTTPError, OSError) as exc:
            raise LLMError(f"{provider} request failed: {exc}") from exc

    if resp.status_code >= 400:
        try:
            detail: Any = resp.json()
        except ValueError:
            detail = resp.text
        raise LLMError(f"{provider} returned HTTP {resp.status_code}: {detail}")

    try:
        return resp.json()
    except ValueError as exc:
        raise LLMError(f"{provider} response was not JSON: {exc}") from exc


# ── Anthropic ─────────────────────────────────────────────────────────


class AnthropicProvider:
    name: ProviderName = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.anthropic.com",
        default_model: str = "claude-haiku-4-5-20251001",
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise LLMUnavailableError("ANTHROPIC_API_KEY is not set")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
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
        cleaned, embedded_system = _strip_system_messages(messages)
        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": cleaned,
        }
        final_system = system or embedded_system
        if final_system:
            payload["system"] = final_system

        data = await _post_json(
            f"{self.base_url}/v1/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload=payload,
            timeout=self._timeout,
            provider=self.name,
        )

        text = ""
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    text = t
                    break

        usage = data.get("usage") or {}
        return LLMCompletion(
            text=text,
            model=data.get("model") or payload["model"],
            provider=self.name,
            stop_reason=data.get("stop_reason"),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            raw=data,
        )


# ── Gemini ────────────────────────────────────────────────────────────


class GeminiProvider:
    name: ProviderName = "gemini"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://generativelanguage.googleapis.com",
        default_model: str = "gemini-2.5-flash",
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise LLMUnavailableError("GEMINI_API_KEY is not set")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
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
        cleaned, embedded_system = _strip_system_messages(messages)

        # Gemini takes a single "contents" array where each entry has
        # role ("user" / "model") and a list of parts. Assistant messages
        # map to role="model".
        contents: list[dict[str, Any]] = []
        for msg in cleaned:
            role = "model" if msg.get("role") == "assistant" else "user"
            content = msg.get("content")
            text = content if isinstance(content, str) else str(content or "")
            contents.append({"role": role, "parts": [{"text": text}]})

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        final_system = system or embedded_system
        if final_system:
            payload["systemInstruction"] = {"parts": [{"text": final_system}]}

        chosen_model = model or self.default_model
        data = await _post_json(
            f"{self.base_url}/v1beta/models/{chosen_model}:generateContent",
            headers={
                "x-goog-api-key": self._api_key,
                "content-type": "application/json",
            },
            payload=payload,
            timeout=self._timeout,
            provider=self.name,
        )

        text = ""
        candidates = data.get("candidates") or []
        if candidates:
            parts = (candidates[0].get("content") or {}).get("parts") or []
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    text = part["text"]
                    break

        usage = data.get("usageMetadata") or {}
        stop_reason = candidates[0].get("finishReason") if candidates else None
        return LLMCompletion(
            text=text,
            model=data.get("modelVersion") or chosen_model,
            provider=self.name,
            stop_reason=stop_reason,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            raw=data,
        )


# ── OpenAI ────────────────────────────────────────────────────────────


class OpenAIProvider:
    name: ProviderName = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com",
        default_model: str = "gpt-4o-mini",
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise LLMUnavailableError("OPENAI_API_KEY is not set")
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
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
        cleaned, embedded_system = _strip_system_messages(messages)

        # OpenAI's Chat Completions API takes system as just another
        # message at the top of the list. Put the explicit `system` arg
        # first, then any user/assistant turns from `messages`.
        final_system = system or embedded_system
        chat_messages: list[dict[str, Any]] = []
        if final_system:
            chat_messages.append({"role": "system", "content": final_system})
        chat_messages.extend(cleaned)

        payload: dict[str, Any] = {
            "model": model or self.default_model,
            "messages": chat_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        data = await _post_json(
            f"{self.base_url}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "content-type": "application/json",
            },
            payload=payload,
            timeout=self._timeout,
            provider=self.name,
        )

        text = ""
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, str):
                text = content

        usage = data.get("usage") or {}
        stop_reason = choices[0].get("finish_reason") if choices else None
        return LLMCompletion(
            text=text,
            model=data.get("model") or payload["model"],
            provider=self.name,
            stop_reason=stop_reason,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            raw=data,
        )


# ── Factory + auto-detect ─────────────────────────────────────────────


def _auto_detect(settings: Settings) -> ProviderName:
    """Pick a provider when ``settings.llm_provider`` is empty.

    Rules:

    - If exactly one key is set → that provider.
    - If multiple keys are set → prefer ``anthropic`` and log a warning
      so operators know they need to set ``LLM_PROVIDER`` explicitly to
      disambiguate.
    - If no keys are set → raise :class:`LLMUnavailableError`.
    """
    configured: list[ProviderName] = []
    if settings.anthropic_api_key:
        configured.append("anthropic")
    if settings.gemini_api_key:
        configured.append("gemini")
    if settings.openai_api_key:
        configured.append("openai")

    if not configured:
        raise LLMUnavailableError(
            "No LLM provider configured — set one of ANTHROPIC_API_KEY, "
            "GEMINI_API_KEY, or OPENAI_API_KEY."
        )
    if len(configured) == 1:
        return configured[0]
    logger.warning(
        "llm_provider_auto_detect_ambiguous",
        configured=configured,
        chosen="anthropic",
        hint="Set LLM_PROVIDER explicitly to disambiguate.",
    )
    # Stable tie-breaker: prefer anthropic → gemini → openai.
    for name in ("anthropic", "gemini", "openai"):
        if name in configured:
            return name  # type: ignore[return-value]
    raise LLMUnavailableError("unreachable: configured is non-empty")


def _build_provider(settings: Settings, name: ProviderName) -> LLMProvider:
    if name == "anthropic":
        return AnthropicProvider(
            api_key=settings.anthropic_api_key,
            base_url=settings.anthropic_base_url,
            default_model=settings.llm_default_model or settings.anthropic_default_model,
            timeout=settings.llm_timeout_seconds,
        )
    if name == "gemini":
        return GeminiProvider(
            api_key=settings.gemini_api_key,
            base_url=settings.gemini_base_url,
            default_model=settings.llm_default_model or settings.gemini_default_model,
            timeout=settings.llm_timeout_seconds,
        )
    if name == "openai":
        return OpenAIProvider(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            default_model=settings.llm_default_model or settings.openai_default_model,
            timeout=settings.llm_timeout_seconds,
        )
    raise LLMUnavailableError(f"Unknown LLM provider: {name!r}")


_client: LLMProvider | None = None
_client_key: tuple[Any, ...] | None = None


def get_llm_client(settings: Settings | None = None) -> LLMProvider:
    """Return a cached provider instance chosen from Settings."""
    global _client, _client_key
    s = settings or get_settings()

    name: ProviderName = (
        s.llm_provider.lower() if s.llm_provider else _auto_detect(s)  # type: ignore[assignment]
    )
    if name not in ("anthropic", "gemini", "openai"):
        raise LLMUnavailableError(
            f"LLM_PROVIDER={s.llm_provider!r} is not one of "
            "'anthropic', 'gemini', 'openai'."
        )

    cache_key: tuple[Any, ...] = (
        name,
        s.anthropic_api_key,
        s.gemini_api_key,
        s.openai_api_key,
        s.llm_default_model,
        s.llm_timeout_seconds,
    )
    if _client is None or _client_key != cache_key:
        _client = _build_provider(s, name)
        _client_key = cache_key
    return _client


def reset_client_cache() -> None:
    """Drop the cached client — used by tests that mutate settings."""
    global _client, _client_key
    _client = None
    _client_key = None


__all__ = [
    "AnthropicProvider",
    "GeminiProvider",
    "LLMCompletion",
    "LLMError",
    "LLMProvider",
    "LLMUnavailableError",
    "OpenAIProvider",
    "ProviderName",
    "_auto_detect",
    "_strip_system_messages",
    "get_llm_client",
    "reset_client_cache",
]
