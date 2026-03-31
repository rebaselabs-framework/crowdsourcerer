"""Task router — maps task types to RebaseKit API workers."""
from __future__ import annotations
import json
from typing import Any

import structlog

from workers.base import RebaseKitClient, WorkerError

logger = structlog.get_logger()

# Credits cost per task type (mirrors @crowdsourcerer/types TASK_CREDITS)
TASK_CREDITS: dict[str, int] = {
    "web_research": 10,
    "entity_lookup": 5,
    "document_parse": 3,
    "data_transform": 2,
    "llm_generate": 1,
    "screenshot": 2,
    "audio_transcribe": 8,
    "pii_detect": 2,
    "code_execute": 3,
    "web_intel": 5,
}


async def execute_task(
    task_type: str,
    task_input: dict[str, Any],
    client: RebaseKitClient,
) -> dict[str, Any]:
    """Route a task to the appropriate RebaseKit API and return structured output."""
    logger.info("executing_task", type=task_type)

    match task_type:
        case "web_research":
            return await _web_research(client, task_input)
        case "entity_lookup":
            return await _entity_lookup(client, task_input)
        case "document_parse":
            return await _document_parse(client, task_input)
        case "data_transform":
            return await _data_transform(client, task_input)
        case "llm_generate":
            return await _llm_generate(client, task_input)
        case "screenshot":
            return await _screenshot(client, task_input)
        case "audio_transcribe":
            return await _audio_transcribe(client, task_input)
        case "pii_detect":
            return await _pii_detect(client, task_input)
        case "code_execute":
            return await _code_execute(client, task_input)
        case "web_intel":
            return await _web_intel(client, task_input)
        case _:
            raise WorkerError(f"Unknown task type: {task_type}", status_code=422)


# ─── Worker implementations ────────────────────────────────────────────────

async def _web_research(client: RebaseKitClient, inp: dict) -> dict:
    # POST /webtask/api/task — AI agent up to 20 steps; requires `url` + `task`
    result = await client.post("/webtask/api/task", {
        "url": inp["url"],
        "task": inp.get("instruction") or inp.get("task", "Extract the main content from this page"),
    })
    return {"raw": result, "summary": result.get("summary") or result.get("result", "")[:500]}


async def _entity_lookup(client: RebaseKitClient, inp: dict) -> dict:
    entity_type = inp.get("entity_type", "company")
    if entity_type == "person":
        # POST /enrich/api/enrich/person — requires name + domain
        result = await client.post("/enrich/api/enrich/person", {
            "name": inp.get("name", ""),
            "domain": inp.get("domain", ""),
        })
    else:
        # POST /enrich/api/enrich/company — single `company` field (domain/URL/name)
        company = inp.get("domain") or inp.get("name") or inp.get("company", "")
        result = await client.post("/enrich/api/enrich/company", {
            "company": company,
            "use_ai": True,
            "include_confidence": False,
        })
    return {"raw": result, "summary": _entity_summary(result)}


def _entity_summary(r: dict) -> str:
    name = r.get("name") or r.get("company_name") or ""
    desc = r.get("description") or r.get("summary") or ""
    return f"{name}: {desc}"[:400]


async def _document_parse(client: RebaseKitClient, inp: dict) -> dict:
    # POST /docparse/api/parse — auto-detect format from URL or base64 content
    payload: dict[str, Any] = {}
    if inp.get("url"):
        payload["url"] = inp["url"]
    if inp.get("base64_content"):
        payload["content_base64"] = inp["base64_content"]  # field is content_base64, not content
    payload["include_tables"] = inp.get("extract_tables", True)   # was extract_tables
    payload["include_markdown"] = True
    result = await client.post("/docparse/api/parse", payload)
    return {"raw": result, "summary": (result.get("text") or result.get("markdown") or "")[:500]}


async def _data_transform(client: RebaseKitClient, inp: dict) -> dict:
    # POST /transform/api/transform — requires input_format, output_format, data (as string)
    data = inp["data"]
    if not isinstance(data, str):
        data = json.dumps(data)
    result = await client.post("/transform/api/transform", {
        "data": data,
        "input_format": inp.get("input_format", "json"),
        "output_format": inp.get("output_format", "json"),
        "field_mapping": inp.get("field_mapping"),
    })
    return {"raw": result}


async def _llm_generate(client: RebaseKitClient, inp: dict) -> dict:
    messages = inp.get("messages", [])
    if inp.get("system_prompt") and not any(m.get("role") == "system" for m in messages):
        messages = [{"role": "system", "content": inp["system_prompt"]}] + list(messages)
    result = await client.post("/llm/v1/chat/completions", {
        "messages": messages,
        "model": inp.get("model", "claude-3-5-haiku-20241022"),
        "temperature": inp.get("temperature", 0.7),
        "max_tokens": inp.get("max_tokens", 2048),
    })
    content = ""
    if result.get("choices"):
        content = result["choices"][0].get("message", {}).get("content", "")
    return {"raw": result, "summary": content[:500]}


async def _screenshot(client: RebaseKitClient, inp: dict) -> dict:
    # POST /screenshot/api/screenshot — Playwright Chromium screenshot
    result = await client.post("/screenshot/api/screenshot", {
        "url": inp["url"],
        "width": inp.get("width", 1280),
        "height": inp.get("height", 800),
        "full_page": inp.get("full_page", False),
        "format": inp.get("format", "png"),
    })
    return {"raw": result, "summary": f"Screenshot of {inp['url']}"}


async def _audio_transcribe(client: RebaseKitClient, inp: dict) -> dict:
    # POST /audio/transcribe — URL-based transcription via OpenAI Whisper
    # Note: only URL-based audio is supported; base64_audio is not supported by this service.
    payload: dict[str, Any] = {
        "url": inp["url"],  # required: URL of the audio file
        "export_format": "json",
    }
    if inp.get("language"):
        payload["language"] = inp["language"]
    result = await client.post("/audio/transcribe", payload)
    return {"raw": result, "summary": (result.get("text") or "")[:500]}


async def _pii_detect(client: RebaseKitClient, inp: dict) -> dict:
    # POST /pii/api/detect — find all PII in text
    result = await client.post("/pii/api/detect", {
        "text": inp["text"],
        "entities": inp.get("entities"),
    })
    count = len(result.get("entities", []))
    return {"raw": result, "summary": f"Found {count} PII entities"}


async def _code_execute(client: RebaseKitClient, inp: dict) -> dict:
    # POST /code/api/execute — run Python or JavaScript code in a sandbox
    result = await client.post("/code/api/execute", {
        "code": inp["code"],
        "language": inp.get("language", "python"),
        "timeout": inp.get("timeout_seconds", 30),
    })
    return {"raw": result, "summary": (result.get("stdout") or result.get("output") or result.get("result", ""))[:500]}


async def _web_intel(client: RebaseKitClient, inp: dict) -> dict:
    # POST /webtask/api/research/auto — self-directed research: search→fetch→synthesize
    result = await client.post("/webtask/api/research/auto", {
        "query": inp["query"],
        "num_sources": min(inp.get("max_results", 5), 10),
    })
    return {"raw": result, "summary": (result.get("summary") or result.get("answer") or "")[:500]}
