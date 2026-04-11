"""Task dispatcher — routes AI task types to their in-process handlers.

Every task type now runs either locally (``workers.local.doc_parse``,
``workers.local.pii``, ``workers.local.code_exec``) or via a direct
Anthropic API call (``workers.local.llm_tasks``). RebaseKit is no
longer in the picture — the ``client`` argument is kept in the
signature for backwards compatibility with older callers and tests
but is not used.
"""

from __future__ import annotations

from typing import Any

import structlog

from workers.base import RebaseKitClient, WorkerError
from workers.local import code_exec, doc_parse, llm_tasks, pii
from core.llm_client import LLMError, LLMUnavailableError
from core.task_types import AI_TASK_CREDITS

logger = structlog.get_logger()


# Re-export so existing callers (``routers/tasks.py``, pipeline dispatcher,
# tests) can keep importing ``TASK_CREDITS`` from here without caring that
# the canonical source is now :mod:`core.task_types`.
TASK_CREDITS = AI_TASK_CREDITS


async def execute_task(
    task_type: str,
    task_input: dict[str, Any],
    client: RebaseKitClient | None = None,
) -> dict[str, Any]:
    """Run *task_type* with the given input and return a structured result.

    The ``client`` parameter is accepted but ignored — it exists for
    backward compatibility with callers that still pass a RebaseKit
    client instance. All handlers now execute in-process.
    """
    del client  # explicitly unused

    logger.info("executing_task", type=task_type)

    try:
        match task_type:
            case "web_research":
                return _wrap(await llm_tasks.web_research(task_input))
            case "document_parse":
                return _wrap(await doc_parse.run(task_input))
            case "data_transform":
                return _wrap(await llm_tasks.data_transform(task_input))
            case "llm_generate":
                result = await llm_tasks.llm_generate(task_input)
                return {"raw": result, "summary": (result.get("text") or "")[:500]}
            case "pii_detect":
                result = pii.run(task_input)
                return {
                    "raw": result,
                    "summary": f"Found {result['count']} PII entities",
                }
            case "code_execute":
                result = await code_exec.run(task_input)
                summary = (result.get("stdout") or "")[:500]
                return {"raw": result, "summary": summary}
            case _:
                raise WorkerError(
                    f"Unknown task type: {task_type}",
                    status_code=422,
                )
    except LLMUnavailableError as exc:
        # Anthropic key missing → surface as 503 so the dispatcher above
        # can refund credits and report the fleet as unavailable.
        raise WorkerError(str(exc), status_code=503) from exc
    except LLMError as exc:
        raise WorkerError(f"LLM call failed: {exc}", status_code=502) from exc
    except ValueError as exc:
        # Input validation errors from the local handlers map to 422.
        raise WorkerError(str(exc), status_code=422) from exc


def _wrap(result: dict[str, Any]) -> dict[str, Any]:
    """Uniform return shape: ``{raw, summary}`` with a text-ish summary."""
    summary_source = (
        result.get("summary")
        or result.get("text")
        or result.get("result")
        or ""
    )
    if not isinstance(summary_source, str):
        summary_source = str(summary_source)
    return {"raw": result, "summary": summary_source[:500]}


__all__ = ["TASK_CREDITS", "execute_task"]
