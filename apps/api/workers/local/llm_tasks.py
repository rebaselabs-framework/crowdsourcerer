"""LLM-backed task handlers.

Three task types, one shared :class:`core.llm_client.AnthropicClient`:

- ``llm_generate``   raw messages → Anthropic → text
- ``data_transform`` wrap user data + instruction in a structured
                     prompt, delegate to llm_generate
- ``web_research``   fetch the URL with httpx, strip HTML to visible
                     text with BeautifulSoup, summarise with llm_generate

All three raise :class:`core.llm_client.LLMUnavailableError` when
``ANTHROPIC_API_KEY`` is unset so the dispatcher can return a clean 503.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog
from bs4 import BeautifulSoup

from core.llm_client import LLMError, LLMUnavailableError, get_llm_client

logger = structlog.get_logger()


# ── llm_generate ──────────────────────────────────────────────────────


async def llm_generate(inp: dict) -> dict:
    text_in = inp.get("messages")
    if not isinstance(text_in, list) or not text_in:
        raise ValueError("llm_generate: 'messages' must be a non-empty array")

    client = get_llm_client()
    system = inp.get("system_prompt")
    # A leading system message in the array also works — Anthropic
    # wants system at the top level though, so promote it.
    if not system:
        for m in text_in:
            if isinstance(m, dict) and m.get("role") == "system":
                system = m.get("content")
                break

    completion = await client.complete(
        messages=text_in,
        system=system,
        model=inp.get("model"),
        max_tokens=int(inp.get("max_tokens") or 2048),
        temperature=float(inp.get("temperature") or 0.7),
    )
    return {
        "text": completion.text,
        "model": completion.model,
        "stop_reason": completion.stop_reason,
        "input_tokens": completion.input_tokens,
        "output_tokens": completion.output_tokens,
    }


# ── data_transform ────────────────────────────────────────────────────


_TRANSFORM_SYSTEM = (
    "You are a data transformation assistant. The user will give you "
    "source data and a natural-language instruction describing the "
    "desired transformation. Produce the transformed data in the "
    "requested output format. Do not include explanatory prose — "
    "return only the transformed data. If the requested format is "
    "JSON, respond with a single JSON value (object or array) with "
    "no markdown fencing."
)


async def data_transform(inp: dict) -> dict:
    if "data" not in inp:
        raise ValueError("data_transform: 'data' is required")
    transform = inp.get("transform") or inp.get("instruction")
    if not isinstance(transform, str) or not transform.strip():
        raise ValueError(
            "data_transform: 'transform' (instruction) is required and must be a string"
        )
    output_format = inp.get("output_format", "json")

    data = inp["data"]
    if not isinstance(data, str):
        data_repr = json.dumps(data, indent=2, default=str)
    else:
        data_repr = data

    user_prompt = (
        f"Source data:\n```\n{data_repr}\n```\n\n"
        f"Transformation: {transform}\n\n"
        f"Output format: {output_format}"
    )

    client = get_llm_client()
    completion = await client.complete(
        system=_TRANSFORM_SYSTEM,
        messages=[{"role": "user", "content": user_prompt}],
        max_tokens=int(inp.get("max_tokens") or 2048),
        temperature=0.1,  # deterministic transforms
    )

    # Try to parse structured output — fall back to raw string.
    result: Any = completion.text.strip()
    if output_format == "json":
        try:
            result = json.loads(completion.text)
        except json.JSONDecodeError:
            pass  # leave as raw string

    return {
        "result": result,
        "format": output_format,
        "model": completion.model,
        "input_tokens": completion.input_tokens,
        "output_tokens": completion.output_tokens,
    }


# ── web_research ──────────────────────────────────────────────────────


_FETCH_TIMEOUT = 20.0
_MAX_PAGE_BYTES = 2 * 1024 * 1024  # 2 MB raw HTML cap
_MAX_EXTRACT_CHARS = 40_000  # post-extraction text fed into the LLM

_WEB_RESEARCH_SYSTEM = (
    "You extract the main content of a web page and answer the user's "
    "research question about it. Be accurate, cite specifics from the "
    "page, and keep the summary under 400 words unless the instruction "
    "explicitly asks for more detail."
)


def _extract_visible_text(html: bytes) -> str:
    """Strip HTML to its visible text using BeautifulSoup. Drops
    ``<script>``, ``<style>``, ``<template>``, and ``<noscript>`` blocks."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "template", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse excessive whitespace.
    lines = [line.strip() for line in text.splitlines()]
    non_empty = [line for line in lines if line]
    joined = "\n".join(non_empty)
    if len(joined) > _MAX_EXTRACT_CHARS:
        joined = joined[:_MAX_EXTRACT_CHARS] + "\n...[truncated]"
    return joined


async def web_research(inp: dict) -> dict:
    url = inp.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("web_research: 'url' is required")
    instruction = (
        inp.get("instruction")
        or inp.get("task")
        or "Summarise the main content of this page."
    )

    try:
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
            headers={
                "user-agent": (
                    "Mozilla/5.0 (compatible; CrowdSorcerer-WebResearch/1.0; "
                    "+https://crowdsourcerer.rebaselabs.online)"
                )
            },
        ) as client:
            resp = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        raise ValueError(f"web_research: failed to fetch {url}: {exc}") from exc

    if resp.status_code >= 400:
        raise ValueError(
            f"web_research: {url} returned HTTP {resp.status_code}"
        )

    body = resp.content[:_MAX_PAGE_BYTES]
    page_text = _extract_visible_text(body)
    if not page_text:
        raise ValueError(f"web_research: no extractable text at {url}")

    llm = get_llm_client()
    completion = await llm.complete(
        system=_WEB_RESEARCH_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    f"URL: {url}\n\n"
                    f"Instruction: {instruction}\n\n"
                    f"Page content:\n```\n{page_text}\n```"
                ),
            }
        ],
        max_tokens=2048,
        temperature=0.2,
    )

    return {
        "url": url,
        "instruction": instruction,
        "summary": completion.text,
        "model": completion.model,
        "extracted_chars": len(page_text),
        "input_tokens": completion.input_tokens,
        "output_tokens": completion.output_tokens,
    }


__all__ = [
    "data_transform",
    "llm_generate",
    "web_research",
    "LLMUnavailableError",
    "LLMError",
]
