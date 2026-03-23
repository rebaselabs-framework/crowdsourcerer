"""Pydantic request/response schemas."""
from __future__ import annotations
from datetime import datetime, date
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
    role: str
    credits: int
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkerProfileOut(BaseModel):
    """Worker-specific profile details."""
    id: UUID
    name: Optional[str]
    role: str
    worker_xp: int
    worker_level: int
    worker_accuracy: Optional[float]
    worker_reliability: Optional[float]
    worker_tasks_completed: int
    worker_streak_days: int

    model_config = {"from_attributes": True}


class BecomeWorkerRequest(BaseModel):
    """Enable worker mode for the current user."""
    skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)


# ─── Tasks ────────────────────────────────────────────────────────────────

ALL_TASK_TYPES = Literal[
    "web_research", "entity_lookup", "document_parse", "data_transform",
    "llm_generate", "screenshot", "audio_transcribe", "pii_detect",
    "code_execute", "web_intel",
    "label_image", "label_text", "rate_quality",
    "verify_fact", "moderate_content", "compare_rank",
    "answer_question", "transcription_review",
]

HUMAN_TASK_TYPES = {
    "label_image", "label_text", "rate_quality",
    "verify_fact", "moderate_content", "compare_rank",
    "answer_question", "transcription_review",
}


class TaskCreateRequest(BaseModel):
    type: ALL_TASK_TYPES
    input: dict[str, Any]
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    metadata: Optional[dict[str, Any]] = None
    webhook_url: Optional[str] = None
    # Human task fields (optional; only used when type is a human task type)
    worker_reward_credits: Optional[int] = Field(None, ge=1, le=10000)
    assignments_required: int = Field(1, ge=1, le=5)
    claim_timeout_minutes: int = Field(30, ge=5, le=480)
    task_instructions: Optional[str] = None


class TaskCreateResponse(BaseModel):
    task_id: UUID
    status: str
    estimated_credits: int
    estimated_duration_ms: Optional[int] = None


class BatchTaskCreateRequest(BaseModel):
    """Create up to 50 tasks in a single API call."""
    tasks: list[TaskCreateRequest] = Field(min_length=1, max_length=50)


class BatchTaskCreateResponse(BaseModel):
    created: list[TaskCreateResponse]
    total_credits: int
    failed: list[dict]  # [{index, error}] for any that failed


class TaskOut(BaseModel):
    id: UUID
    type: str
    status: str
    priority: str
    execution_mode: str
    input: dict[str, Any]
    output: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    credits_used: Optional[int] = None
    duration_ms: Optional[int] = None
    metadata: Optional[dict[str, Any]] = None
    worker_reward_credits: Optional[int] = None
    assignments_required: int = 1
    assignments_completed: int = 0
    task_instructions: Optional[str] = None
    is_gold_standard: bool = False
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


# ─── Worker / Task Assignments ─────────────────────────────────────────────

class TaskAssignmentOut(BaseModel):
    id: UUID
    task_id: UUID
    worker_id: UUID
    status: str
    response: Optional[dict[str, Any]] = None
    worker_note: Optional[str] = None
    earnings_credits: int
    xp_earned: int
    claimed_at: datetime
    submitted_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
    timeout_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class TaskAssignmentWithTaskOut(TaskAssignmentOut):
    """Assignment with embedded task details (for worker views)."""
    task: TaskOut


class WorkerTaskClaimResponse(BaseModel):
    assignment_id: UUID
    task_id: UUID
    timeout_at: datetime
    message: str = "Task claimed successfully. You have until timeout_at to submit."


class WorkerTaskSubmitRequest(BaseModel):
    response: dict[str, Any]  # Task-type-specific answer structure
    worker_note: Optional[str] = None


class WorkerTaskSubmitResponse(BaseModel):
    assignment_id: UUID
    status: str
    earnings_credits: int
    xp_earned: int
    message: str


class WorkerStatsOut(BaseModel):
    tasks_completed: int
    tasks_active: int
    tasks_released: int
    total_earnings_credits: int
    accuracy: Optional[float]
    reliability: Optional[float]
    level: int
    xp: int
    xp_to_next_level: int
    streak_days: int


class MarketplaceTaskOut(BaseModel):
    """Minimal task view for the worker marketplace."""
    id: UUID
    type: str
    priority: str
    reward_credits: int
    estimated_minutes: int
    assignments_required: int
    assignments_completed: int
    slots_available: int
    task_instructions: Optional[str] = None
    created_at: datetime


class PaginatedMarketplaceTasks(BaseModel):
    items: list[MarketplaceTaskOut]
    total: int
    page: int
    page_size: int
    has_next: bool


# ─── Leaderboard ──────────────────────────────────────────────────────────

class LeaderboardEntryOut(BaseModel):
    rank: int
    user_id: UUID
    name: Optional[str]
    worker_level: int
    worker_xp: int
    worker_tasks_completed: int
    worker_accuracy: Optional[float]
    worker_reliability: Optional[float]
    worker_streak_days: int


class LeaderboardOut(BaseModel):
    period: str   # "all_time" | "weekly"
    category: str # "xp" | "tasks" | "earnings"
    entries: list[LeaderboardEntryOut]
    generated_at: datetime


# ─── Badges ───────────────────────────────────────────────────────────────

class BadgeOut(BaseModel):
    badge_id: str
    name: str
    description: str
    icon: str        # emoji
    earned_at: Optional[datetime] = None
    earned: bool = False


class WorkerBadgesOut(BaseModel):
    earned: list[BadgeOut]
    locked: list[BadgeOut]
    total_earned: int


# ─── Daily Challenges ─────────────────────────────────────────────────────

class DailyChallengeOut(BaseModel):
    id: UUID
    challenge_date: date
    task_type: str
    title: str
    description: Optional[str]
    bonus_xp: int
    bonus_credits: int
    target_count: int

    model_config = {"from_attributes": True}


class DailyChallengeProgressOut(BaseModel):
    challenge: DailyChallengeOut
    tasks_completed: int
    bonus_claimed: bool
    is_complete: bool      # tasks_completed >= target_count
    tasks_remaining: int


# ─── Submission Review ────────────────────────────────────────────────────

class SubmissionWorkerOut(BaseModel):
    """Minimal worker info attached to a submission."""
    id: UUID
    name: Optional[str]
    worker_level: int
    worker_accuracy: Optional[float]
    worker_tasks_completed: int

    model_config = {"from_attributes": True}


class SubmissionOut(BaseModel):
    """Worker submission as seen by the requester."""
    id: UUID
    task_id: UUID
    worker: SubmissionWorkerOut
    status: str
    response: Optional[dict[str, Any]] = None
    worker_note: Optional[str] = None
    earnings_credits: int
    xp_earned: int
    claimed_at: datetime
    submitted_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SubmissionReviewRequest(BaseModel):
    reason: Optional[str] = None  # Optional rejection reason or approval note


class SubmissionReviewResponse(BaseModel):
    assignment_id: UUID
    status: str
    message: str


# ─── Quality Control ──────────────────────────────────────────────────────

class GoldStandardCreateRequest(BaseModel):
    """Mark a task as gold standard by providing the expected answer."""
    task_id: UUID
    gold_answer: dict[str, Any]


class QualityReportOut(BaseModel):
    worker_id: UUID
    name: Optional[str]
    tasks_evaluated: int
    tasks_correct: int
    accuracy: float
    reliability: Optional[float]
    worker_level: int
    worker_xp: int


# ─── Payout ───────────────────────────────────────────────────────────────

class PayoutRequestCreate(BaseModel):
    credits_requested: int
    payout_method: str  # paypal | bank_transfer | crypto
    payout_details: dict[str, Any]  # {"email": "..."} or {"address": "..."}


class PayoutRequestOut(BaseModel):
    id: UUID
    worker_id: UUID
    credits_requested: int
    usd_amount: float
    status: str
    payout_method: str
    payout_details: dict[str, Any]
    admin_note: Optional[str]
    processed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PayoutReviewRequest(BaseModel):
    status: str  # processing | paid | rejected
    admin_note: Optional[str] = None


class PayoutListOut(BaseModel):
    items: list[PayoutRequestOut]
    total: int


# ─── Referral ─────────────────────────────────────────────────────────────

class ReferralStatsOut(BaseModel):
    referral_code: str
    referral_url: str
    total_referrals: int
    pending_bonus_credits: int  # credits_pending from referrals
    paid_bonus_credits: int     # already confirmed bonus credits


class ReferralOut(BaseModel):
    id: UUID
    referred_email: Optional[str]  # masked for privacy
    bonus_paid: bool
    referrer_bonus_credits: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Notifications ────────────────────────────────────────────────────────

class NotificationOut(BaseModel):
    id: UUID
    type: str
    title: str
    body: str
    link: Optional[str]
    is_read: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationListOut(BaseModel):
    items: list[NotificationOut]
    total: int
    unread_count: int


class UnreadCountOut(BaseModel):
    unread_count: int


# ─── Health ───────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    workers_online: int
