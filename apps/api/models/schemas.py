"""Pydantic request/response schemas."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, model_validator


# ─── Auth ─────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: Optional[str] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


# ─── Users ────────────────────────────────────────────────────────────────

class UserOut(BaseModel):
    id: UUID
    email: str
    name: Optional[str]
    plan: str
    credits: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Tasks ────────────────────────────────────────────────────────────────

class TaskCreateRequest(BaseModel):
    type: Literal[
        "web_research", "entity_lookup", "document_parse", "data_transform",
        "llm_generate", "screenshot", "audio_transcribe", "pii_detect",
        "code_execute", "web_intel",
    ]
    input: dict[str, Any]
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    metadata: Optional[dict[str, Any]] = None
    webhook_url: Optional[str] = None


class TaskCreateResponse(BaseModel):
    task_id: UUID
    status: str
    estimated_credits: int
    estimated_duration_ms: Optional[int] = None


class TaskOut(BaseModel):
    id: UUID
    type: str
    status: str
    priority: str
    input: dict[str, Any]
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    credits_used: Optional[int] = None
    duration_ms: Optional[int] = None
    metadata: Optional[dict[str, Any]] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class PaginatedTasks(BaseModel):
    items: list[TaskOut]
    total: int
    page: int
    page_size: int
    has_next: bool


# ─── API Keys ─────────────────────────────────────────────────────────────

class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(default_factory=list)


class ApiKeyOut(BaseModel):
    id: UUID
    name: str
    prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class ApiKeyCreateResponse(BaseModel):
    id: UUID
    key: str  # plaintext — only returned once
    name: str
    created_at: datetime


# ─── Credits ──────────────────────────────────────────────────────────────

class CreditBalanceOut(BaseModel):
    available: int
    reserved: int
    total_used: int
    plan: str


class CreditTransactionOut(BaseModel):
    id: UUID
    task_id: Optional[UUID] = None
    amount: int
    type: str
    description: str
    created_at: datetime

    model_config = {"from_attributes": True}


class PaginatedTransactions(BaseModel):
    items: list[CreditTransactionOut]
    total: int
    page: int
    page_size: int
    has_next: bool


# ─── Checkout ─────────────────────────────────────────────────────────────

class CheckoutRequest(BaseModel):
    credits: int = Field(ge=100, le=100_000)
    success_url: str
    cancel_url: str


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str


# ─── Health ───────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    workers_online: int
