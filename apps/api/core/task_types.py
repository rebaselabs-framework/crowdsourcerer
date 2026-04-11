"""Single source of truth for task type metadata.

Every other module that needs to know:

- which task types exist
- whether a type is human-submittable or pipeline-AI-only
- how much a type costs in credits
- how to label or iconify it for UI consumers
- whether an AI type is a local handler or needs an LLM provider key

should import from this module. Keeping the list in one place means
adding or renaming a task type is a single diff instead of seven.

The :data:`TASK_METADATA` mapping is the canonical record; every other
constant in this module is derived from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

ExecutionMode = Literal["human", "ai"]
AISubkind = Literal["local", "llm"]


@dataclass(frozen=True, slots=True)
class TaskTypeMeta:
    id: str
    label: str
    icon: str
    execution_mode: ExecutionMode
    base_credits: int
    description: str
    # ``None`` for human tasks. For AI tasks:
    #   "local" = in-process handler, always available
    #   "llm"   = requires an LLM provider key (Anthropic/Gemini/OpenAI)
    ai_subkind: AISubkind | None


_HUMAN: tuple[TaskTypeMeta, ...] = (
    TaskTypeMeta(
        "label_image", "Label Image", "🖼️",
        "human", 3,
        "Bounding boxes, segmentation, or classification on an image.",
        None,
    ),
    TaskTypeMeta(
        "label_text", "Label Text", "🏷️",
        "human", 2,
        "Sentiment, intent, categories, or spam detection on text.",
        None,
    ),
    TaskTypeMeta(
        "rate_quality", "Rate Quality", "⭐",
        "human", 2,
        "Score content on a 1–5 (or custom) scale with a written critique.",
        None,
    ),
    TaskTypeMeta(
        "verify_fact", "Verify Fact", "✅",
        "human", 3,
        "Check a claim against sources — true / false / unverifiable.",
        None,
    ),
    TaskTypeMeta(
        "moderate_content", "Moderate Content", "🛡️",
        "human", 2,
        "Approve, reject, or escalate user-submitted content.",
        None,
    ),
    TaskTypeMeta(
        "compare_rank", "Compare & Rank", "📊",
        "human", 2,
        "Pick A vs B (or rank N) on any criterion.",
        None,
    ),
    TaskTypeMeta(
        "answer_question", "Answer Question", "💬",
        "human", 4,
        "Open-ended Q&A with optional context.",
        None,
    ),
    TaskTypeMeta(
        "transcription_review", "Review Transcript", "📝",
        "human", 5,
        "Correct an AI-generated transcript.",
        None,
    ),
)


_AI: tuple[TaskTypeMeta, ...] = (
    TaskTypeMeta(
        "llm_generate", "LLM Generate", "🤖",
        "ai", 1,
        "Direct LLM completion via the configured provider.",
        "llm",
    ),
    TaskTypeMeta(
        "data_transform", "Data Transform", "🔄",
        "ai", 2,
        "LLM-backed structured data transformation with a natural-language instruction.",
        "llm",
    ),
    TaskTypeMeta(
        "pii_detect", "PII Detect", "🔒",
        "ai", 2,
        "Regex detector for email, phone, SSN, credit card, and more.",
        "local",
    ),
    TaskTypeMeta(
        "document_parse", "Document Parse", "📄",
        "ai", 3,
        "Extract text from PDF / DOCX / XLSX via pypdf / python-docx / openpyxl.",
        "local",
    ),
    TaskTypeMeta(
        "code_execute", "Code Execute", "⚡",
        "ai", 3,
        "Sandboxed Python subprocess with rlimits and a temp working directory.",
        "local",
    ),
    TaskTypeMeta(
        "web_research", "Web Research", "🌐",
        "ai", 10,
        "Fetch a URL, extract visible text with BeautifulSoup, and summarise with the LLM.",
        "llm",
    ),
)


TASK_METADATA: Mapping[str, TaskTypeMeta] = MappingProxyType(
    {t.id: t for t in (*_HUMAN, *_AI)}
)


# ── Derived sets ─────────────────────────────────────────────────────

HUMAN_TASK_TYPES: frozenset[str] = frozenset(t.id for t in _HUMAN)
AI_TASK_TYPES: frozenset[str] = frozenset(t.id for t in _AI)
ALL_TASK_TYPES: frozenset[str] = HUMAN_TASK_TYPES | AI_TASK_TYPES

LOCAL_TASK_TYPES: frozenset[str] = frozenset(
    t.id for t in _AI if t.ai_subkind == "local"
)
LLM_TASK_TYPES: frozenset[str] = frozenset(
    t.id for t in _AI if t.ai_subkind == "llm"
)


# ── Derived credit maps ──────────────────────────────────────────────

HUMAN_TASK_BASE_CREDITS: Mapping[str, int] = MappingProxyType(
    {t.id: t.base_credits for t in _HUMAN}
)
AI_TASK_CREDITS: Mapping[str, int] = MappingProxyType(
    {t.id: t.base_credits for t in _AI}
)


__all__ = [
    "AI_TASK_CREDITS",
    "AI_TASK_TYPES",
    "AISubkind",
    "ALL_TASK_TYPES",
    "ExecutionMode",
    "HUMAN_TASK_BASE_CREDITS",
    "HUMAN_TASK_TYPES",
    "LLM_TASK_TYPES",
    "LOCAL_TASK_TYPES",
    "TASK_METADATA",
    "TaskTypeMeta",
]
