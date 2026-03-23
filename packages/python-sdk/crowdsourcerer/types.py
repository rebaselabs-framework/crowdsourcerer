"""Pydantic models for CrowdSorcerer API objects."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ─── Enums ────────────────────────────────────────────────────────────────

TaskType = Literal[
    # AI task types
    "web_research", "entity_lookup", "document_parse", "data_transform",
    "llm_generate", "screenshot", "audio_transcribe", "pii_detect",
    "code_execute", "web_intel",
    # Human task types
    "label_image", "label_text", "rate_quality", "verify_fact",
    "moderate_content", "compare_rank", "answer_question", "transcription_review",
]

TaskStatus = Literal["pending", "queued", "running", "open", "assigned", "completed", "failed", "cancelled"]
TaskPriority = Literal["low", "normal", "high", "urgent"]
ExecutionMode = Literal["ai", "human"]

# Credits charged per task type
TASK_CREDITS: Dict[str, int] = {
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


# ─── Task models ──────────────────────────────────────────────────────────

class Task(BaseModel):
    id: UUID
    type: str
    status: str
    priority: str
    execution_mode: str
    input: Dict[str, Any]
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    credits_used: Optional[int] = None
    duration_ms: Optional[int] = None
    webhook_url: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None


class TaskCreateRequest(BaseModel):
    type: str
    input: Dict[str, Any]
    priority: TaskPriority = "normal"
    webhook_url: Optional[str] = None
    # Human task fields
    worker_reward_credits: Optional[int] = None
    assignments_required: Optional[int] = None
    task_instructions: Optional[str] = None
    claim_timeout_minutes: Optional[int] = None


class TaskCreateResponse(BaseModel):
    id: UUID
    type: str
    status: str
    credits_used: int
    execution_mode: str
    created_at: datetime


class BatchTaskCreateRequest(BaseModel):
    tasks: List[TaskCreateRequest] = Field(..., min_length=1, max_length=50)


class BatchTaskCreateResponse(BaseModel):
    created: List[TaskCreateResponse]
    errors: List[Dict[str, Any]]
    summary: Dict[str, int]


class PaginatedTasks(BaseModel):
    items: List[Task]
    total: int
    limit: int
    offset: int


# ─── Credit models ────────────────────────────────────────────────────────

class CreditBalance(BaseModel):
    available: int
    total_used: int
    total_purchased: int


class CreditTransaction(BaseModel):
    id: UUID
    amount: int
    type: str
    description: str
    created_at: datetime


# ─── User models ──────────────────────────────────────────────────────────

class User(BaseModel):
    id: UUID
    email: str
    name: Optional[str] = None
    plan: str
    role: str
    credits: int
    created_at: datetime


# ─── API Key models ───────────────────────────────────────────────────────

class ApiKey(BaseModel):
    id: UUID
    name: str
    key_prefix: str
    scopes: List[str]
    last_used_at: Optional[datetime] = None
    created_at: datetime


class ApiKeyCreateRequest(BaseModel):
    name: str
    scopes: List[str] = Field(default_factory=list)


class ApiKeyCreateResponse(BaseModel):
    id: UUID
    name: str
    key: str  # Full key — shown only once
    key_prefix: str
    scopes: List[str]
    created_at: datetime
