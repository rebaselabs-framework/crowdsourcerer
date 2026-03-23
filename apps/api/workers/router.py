"""Task router — maps task types to RebaseKit API workers."""
from __future__ import annotations
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
    result = await client.post("/webtask/task", {
        "url": inp["url"],
        "instruction": inp.get("instruction", "Extract the main content"),
        "extract_tables": inp.get("extract_tables", False),
        "extract_links": inp.get("extract_links", False),
        "wait_for_selector": inp.get("wait_for_selector"),
    })
    return {"raw": result, "summary": result.get("summary") or result.get("content", "")[:500]}


async def _entity_lookup(client: RebaseKitClient, inp: dict) -> dict:
    payload: dict[str, Any] = {"entity_type": inp.get("entity_type", "company")}
    if inp.get("name"):
        payload["name"] = inp["name"]
    if inp.get("domain"):
        payload["domain"] = inp["domain"]
    if inp.get("linkedin_url"):
        payload["linkedin_url"] = inp["linkedin_url"]
    if inp.get("enrich_fields"):
        payload["fields"] = inp["enrich_fields"]
    result = await client.post("/entity-enrichment/enrich", payload)
    return {"raw": result, "summary": _entity_summary(result)}


def _entity_summary(r: dict) -> str:
    name = r.get("name") or r.get("company_name") or ""
    desc = r.get("description") or r.get("summary") or ""
    return f"{name}: {desc}"[:400]


async def _document_parse(client: RebaseKitClient, inp: dict) -> dict:
    payload: dict[str, Any] = {}
    if inp.get("url"):
        payload["url"] = inp["url"]
    if inp.get("base64_content"):
        payload["content"] = inp["base64_content"]
        payload["mime_type"] = inp.get("mime_type", "application/pdf")
    payload["extract_tables"] = inp.get("extract_tables", True)
    payload["extract_images"] = inp.get("extract_images", False)
    result = await client.post("/doc-parse/parse", payload)
    return {"raw": result, "summary": (result.get("text") or "")[:500]}


async def _data_transform(client: RebaseKitClient, inp: dict) -> dict:
    result = await client.post("/data-transform/transform", {
        "data": inp["data"],
        "transform": inp["transform"],
        "output_format": inp.get("output_format", "json"),
    })
    return {"raw": result}


async def _llm_generate(client: RebaseKitClient, inp: dict) -> dict:
    messages = inp.get("messages", [])
    if inp.get("system_prompt") and not any(m.get("role") == "system" for m in messages):
        messages = [{"role": "system", "content": inp["system_prompt"]}] + list(messages)
    result = await client.post("/llm-router/v1/chat/completions", {
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
    result = await client.post("/screenshot/screenshot", {
        "url": inp["url"],
        "width": inp.get("width", 1280),
        "height": inp.get("height", 800),
        "full_page": inp.get("full_page", False),
        "format": inp.get("format", "png"),
        "wait_for_selector": inp.get("wait_for_selector"),
    })
    return {"raw": result, "summary": f"Screenshot of {inp['url']}"}


async def _audio_transcribe(client: RebaseKitClient, inp: dict) -> dict:
    payload: dict[str, Any] = {
        "language": inp.get("language"),
        "diarize": inp.get("diarize", False),
    }
    if inp.get("url"):
        payload["url"] = inp["url"]
    if inp.get("base64_audio"):
        payload["audio"] = inp["base64_audio"]
    result = await client.post("/audio-to-text/transcribe", payload)
    return {"raw": result, "summary": (result.get("text") or "")[:500]}


async def _pii_detect(client: RebaseKitClient, inp: dict) -> dict:
    result = await client.post("/pii/detect", {
        "text": inp["text"],
        "entities": inp.get("entities"),
        "mask": inp.get("mask", False),
        "vault": inp.get("vault", False),
    })
    count = len(result.get("entities", []))
    return {"raw": result, "summary": f"Found {count} PII entities"}


async def _code_execute(client: RebaseKitClient, inp: dict) -> dict:
    result = await client.post("/code-exec/execute", {
        "code": inp["code"],
        "language": inp.get("language", "python"),
        "timeout": inp.get("timeout_seconds", 30),
        "stdin": inp.get("stdin"),
    })
    return {"raw": result, "summary": (result.get("stdout") or result.get("output") or "")[:500]}


async def _web_intel(client: RebaseKitClient, inp: dict) -> dict:
    result = await client.post("/web-intel/intel", {
        "query": inp["query"],
        "sources": inp.get("sources"),
        "max_results": inp.get("max_results", 10),
    })
    return {"raw": result, "summary": (result.get("summary") or "")[:500]}
