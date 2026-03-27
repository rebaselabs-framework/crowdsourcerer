"""Pydantic request/response schemas."""
from __future__ import annotations
from datetime import datetime, date
from typing import Any, Literal, Optional, Union
from uuid import UUID

from pydantic import AliasChoices, AnyHttpUrl, BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


# ─── Auth ─────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    name: Optional[str] = None
    role: Literal["requester", "worker"] = "requester"


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
    availability_status: Optional[str] = "available"
    email_verified: bool = False

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
    worker_skill_interests: list[str] = Field(default_factory=list)

    model_config = {"from_attributes": True}

    @classmethod
    def model_validate(cls, obj, **kwargs):  # type: ignore[override]
        # worker_skill_interests may be None in DB (pre-migration rows)
        if hasattr(obj, "worker_skill_interests") and obj.worker_skill_interests is None:
            obj.worker_skill_interests = []
        return super().model_validate(obj, **kwargs)


# Valid human task types for interest declarations
HUMAN_TASK_TYPES_SET = {
    "label_image", "label_text", "rate_quality", "verify_fact",
    "moderate_content", "compare_rank", "answer_question", "transcription_review",
}


class BecomeWorkerRequest(BaseModel):
    """Enable worker mode for the current user."""
    skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)


class WorkerSkillInterestsUpdate(BaseModel):
    """Update the worker's declared skill interests (task types they want to work on)."""
    interests: list[str] = Field(
        ...,
        description="Task type identifiers the worker is interested in",
    )

    @field_validator("interests")
    @classmethod
    def validate_interests(cls, v: list[str]) -> list[str]:
        valid = HUMAN_TASK_TYPES_SET
        invalid = [x for x in v if x not in valid]
        if invalid:
            raise ValueError(f"Unknown task types: {invalid}. Valid: {sorted(valid)}")
        return list(dict.fromkeys(v))  # deduplicate preserving order


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


CONSENSUS_STRATEGIES = Literal["any_first", "majority_vote", "unanimous", "requester_review"]


class TaskCreateRequest(BaseModel):
    type: ALL_TASK_TYPES
    input: dict[str, Any]
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    metadata: Optional[dict[str, Any]] = None
    webhook_url: Optional[str] = None
    # Which events to subscribe to. NULL/empty → ["task.completed"]
    webhook_events: Optional[list[str]] = None
    org_id: Optional[UUID] = None  # Create task under an org's credit pool
    # Human task fields (optional; only used when type is a human task type)
    worker_reward_credits: Optional[int] = Field(None, ge=1, le=10000)
    assignments_required: int = Field(1, ge=1, le=10)
    claim_timeout_minutes: int = Field(30, ge=5, le=480)
    task_instructions: Optional[str] = None
    # Consensus strategy (only relevant when assignments_required > 1)
    consensus_strategy: CONSENSUS_STRATEGIES = "any_first"
    # Minimum worker skill level (1–5) required to claim this task
    min_skill_level: Optional[int] = Field(None, ge=1, le=5)
    # Labels / tags (max 20, each max 50 chars)
    tags: Optional[list[str]] = Field(None, max_length=20)
    # Deferred execution — ISO datetime in the future to schedule task
    scheduled_at: Optional[datetime] = None


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


class BulkActionRequest(BaseModel):
    """Perform a bulk action on multiple tasks."""
    task_ids: list[UUID] = Field(min_length=1, max_length=100)
    action: Literal["cancel", "retry"]


class BulkActionResult(BaseModel):
    succeeded: list[str]
    failed: list[dict]  # [{task_id, reason}]
    action: str


class BulkCancelRequest(BaseModel):
    """Bulk-cancel up to 100 tasks."""
    task_ids: list[UUID] = Field(min_length=1, max_length=100)


class BulkCancelResult(BaseModel):
    cancelled: int
    skipped: int
    task_ids: list[str]  # IDs of tasks that were successfully cancelled


class BulkArchiveRequest(BaseModel):
    """Bulk-archive up to 100 tasks (must be in terminal state)."""
    task_ids: list[UUID] = Field(min_length=1, max_length=100)


class BulkArchiveResult(BaseModel):
    archived: int
    skipped: int
    task_ids: list[str]  # IDs of tasks that were successfully archived


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
    # Use only "task_metadata" as the ORM alias. Never use "metadata" — on SQLAlchemy
    # declarative models, `obj.metadata` returns the SQLAlchemy MetaData object (not our
    # column data), which causes Pydantic serialization to blow up with a 500.
    metadata: Optional[dict[str, Any]] = Field(None, validation_alias="task_metadata")
    worker_reward_credits: Optional[int] = None
    assignments_required: int = 1
    assignments_completed: int = 0
    task_instructions: Optional[str] = None
    is_gold_standard: bool = False
    consensus_strategy: str = "any_first"
    dispute_status: Optional[str] = None
    winning_assignment_id: Optional[UUID] = None
    org_id: Optional[UUID] = None
    tags: Optional[list[str]] = None
    scheduled_at: Optional[datetime] = None
    priority_escalated_at: Optional[datetime] = None
    cached: bool = False
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class PaginatedTasks(BaseModel):
    items: list[TaskOut]
    total: int
    page: int
    page_size: int
    has_next: bool


# ─── API Keys ─────────────────────────────────────────────────────────────

class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(
        default_factory=list,
        description=(
            "List of scope strings (e.g. 'tasks:read', 'tasks:write'). "
            "Empty list = full access (legacy behaviour)."
        ),
    )


class ApiKeyOut(BaseModel):
    id: UUID
    name: str
    key_prefix: str
    scopes: list[str]
    created_at: datetime
    last_used_at: Optional[datetime] = None
    rate_limit_rpm: Optional[int] = None
    rate_limit_daily: Optional[int] = None

    model_config = {"from_attributes": True}


class ApiKeyCreateResponse(BaseModel):
    id: UUID
    key: str  # plaintext — only returned once
    name: str
    scopes: list[str] = Field(default_factory=list)
    created_at: datetime


class ApiKeyRateLimitUpdate(BaseModel):
    """PATCH body to configure per-key rate limits. Null = revert to plan default."""
    rate_limit_rpm: Optional[int] = Field(None, ge=1, le=10_000, description="Max requests/minute (null = plan default)")
    rate_limit_daily: Optional[int] = Field(None, ge=1, le=1_000_000, description="Max requests/day (null = plan default)")


class ApiKeyRateStatusOut(BaseModel):
    key_id: UUID
    key_prefix: str
    rpm: dict     # {limit, used, remaining, unlimited}
    daily: dict   # {limit, used, remaining, unlimited}


class CreditAlertOut(BaseModel):
    threshold: Optional[int]  # None = disabled
    alert_fired: bool         # True if alert has been sent and not yet reset


class CreditAlertUpdate(BaseModel):
    threshold: Optional[int] = Field(None, ge=0, le=1_000_000, description="Alert when credits drop below this. Null = disable.")


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
    success_url: AnyHttpUrl
    cancel_url: AnyHttpUrl


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
    requester_note: Optional[str] = None
    reviewed_at: Optional[datetime] = None

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
    streak_multiplier: float = 1.0  # XP multiplier applied (1.0 = no bonus)
    streak_days: int = 0             # Worker's streak at time of submission
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
    streak_at_risk: bool = False        # True if no task completed today (streak could break)
    last_active_date: Optional[str] = None  # ISO date string of last activity (YYYY-MM-DD)


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
    # Skill-based matching fields (populated when using /v1/worker/tasks/feed)
    match_score: Optional[float] = None     # 0.0–1.0; None = not yet computed
    min_skill_level: Optional[int] = None   # Required proficiency (1–5) set by requester
    # Application-mode fields
    application_mode: bool = False          # True = requires proposal; False = direct claim
    user_applied: bool = False              # True = requesting worker already has a pending application


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
    profile_public: bool = True  # if False, link to profile is hidden


class LeaderboardOut(BaseModel):
    period: str    # "all_time" | "weekly"
    category: str  # "xp" | "tasks" | "earnings"
    entries: list[LeaderboardEntryOut]
    generated_at: datetime
    caller_id: Optional[str] = None  # set when caller is authenticated


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
    requester_note: Optional[str] = None
    reviewed_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class SubmissionReviewRequest(BaseModel):
    reason: Optional[str] = None  # Optional feedback/note from requester (stored as requester_note)


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
    credits_requested: int = Field(ge=1, le=10_000_000)
    payout_method: Literal["paypal", "bank_transfer", "crypto"]
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


# ─── Worker Skills ────────────────────────────────────────────────────────

PROFICIENCY_LABELS = {
    1: "Novice",
    2: "Learner",
    3: "Competent",
    4: "Proficient",
    5: "Expert",
}


class WorkerSkillOut(BaseModel):
    task_type: str
    tasks_completed: int
    tasks_approved: int
    tasks_rejected: int
    accuracy: Optional[float]
    avg_response_minutes: Optional[float]
    credits_earned: int
    proficiency_level: int
    proficiency_label: str
    last_task_at: Optional[datetime]
    verified: bool = False
    verified_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class WorkerSkillsOut(BaseModel):
    skills: list[WorkerSkillOut]
    top_skill: Optional[str]          # task_type with highest proficiency
    strongest_category: Optional[str]  # "human" or "ai"
    verified_count: int = 0           # number of verified skills


# ─── Worker Recommendations ──────────────────────────────────────────────────

class TaskTypeRecommendation(BaseModel):
    """A recommended task type with earnings and growth context."""
    task_type: str
    label: str                          # Human-readable name e.g. "Web Research"
    category: str                       # "ai" or "human"
    reason: str                         # Short explanation of why recommended
    proficiency_level: int              # Current level 1–5 (0 if never tried)
    accuracy: Optional[float]           # Acceptance rate 0.0–1.0; None if untried
    tasks_completed: int
    avg_credits_per_task: float         # Credits earned per completed task
    estimated_weekly_credits: int       # At 20 tasks/day × 5 days × acceptance_rate
    estimated_weekly_usd: float
    is_verified: bool
    next_level_tasks_needed: int        # Tasks to reach next proficiency threshold; 0 = maxed
    tasks_to_verification: int          # Approved tasks still needed; 0 = already verified or ineligible


class WorkerRecommendationsOut(BaseModel):
    """Personalised task-type recommendations with earnings potential."""
    best_types: list[TaskTypeRecommendation]   # Top earners — up to 5
    try_next: list[TaskTypeRecommendation]     # Untried types likely to suit them — up to 3
    weekly_earnings_potential: int             # Credits/week focused on best_types
    weekly_earnings_potential_usd: float
    current_weekly_rate: int                   # Credits actually earned last 7 days
    insights: list[str]                        # Actionable tips


# ─── Task Dependencies ──────────────────────────────────────────────────────

class TaskDependencyOut(BaseModel):
    id: UUID
    task_id: UUID
    depends_on_id: UUID
    depends_on_title: Optional[str] = None
    depends_on_status: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AddDependencyRequest(BaseModel):
    depends_on_id: UUID


# ─── Task Analytics ────────────────────────────────────────────────────────

class AssignmentAnalyticsRow(BaseModel):
    worker_id: UUID
    worker_name: Optional[str]
    status: str
    submitted_at: Optional[datetime]
    response_minutes: Optional[float]
    earnings_credits: int
    is_accurate: Optional[bool]  # vs gold_answer, if gold standard


class TaskAnalyticsOut(BaseModel):
    task_id: UUID
    task_type: str
    title: Optional[str]
    status: str
    execution_mode: str
    total_assignments: int
    approved_count: int
    rejected_count: int
    pending_count: int
    avg_response_minutes: Optional[float]
    total_credits_paid: int
    is_gold_standard: bool
    accuracy_rate: Optional[float]   # % accurate vs gold answer
    response_distribution: dict      # {answer_value: count}
    assignments: list[AssignmentAnalyticsRow]


# ─── Disputes / Consensus ─────────────────────────────────────────────────

class ConsensusVoteOut(BaseModel):
    """Vote breakdown for a specific answer in a majority-vote task."""
    response_key: str    # JSON representation of the response value
    count: int
    percentage: float
    assignment_ids: list[UUID]


class ConsensusStateOut(BaseModel):
    """Consensus status for a multi-worker task."""
    task_id: UUID
    strategy: str
    assignments_required: int
    assignments_submitted: int
    consensus_reached: bool
    dispute_status: Optional[str]      # None | "disputed" | "resolved"
    winning_assignment_id: Optional[UUID]
    votes: list[ConsensusVoteOut]      # Vote breakdown (for majority_vote / unanimous)


class DisputeResolveRequest(BaseModel):
    winning_assignment_id: UUID
    resolution_note: Optional[str] = None


class DisputeResolveResponse(BaseModel):
    task_id: UUID
    winning_assignment_id: UUID
    status: str
    message: str


# ─── Task Export ──────────────────────────────────────────────────────────

class TaskExportRow(BaseModel):
    """Single row in a task export."""
    task_id: str
    type: str
    status: str
    execution_mode: str
    priority: str
    created_at: str
    completed_at: Optional[str]
    credits_used: Optional[int]
    input_summary: str
    output_summary: Optional[str]
    assignments_required: int
    assignments_completed: int
    dispute_status: Optional[str]
    org_id: Optional[str]


# ─── Organizations ────────────────────────────────────────────────────────

class OrgCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    slug: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9\-]+$")
    description: Optional[str] = None


class OrgOut(BaseModel):
    id: UUID
    name: str
    slug: str
    owner_id: UUID
    credits: int
    plan: str
    description: Optional[str]
    avatar_url: Optional[str]
    member_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class OrgMemberOut(BaseModel):
    id: UUID
    org_id: UUID
    user_id: UUID
    name: Optional[str]
    email: Optional[str]
    role: str
    joined_at: datetime

    model_config = {"from_attributes": True}


class OrgInviteRequest(BaseModel):
    email: EmailStr
    role: Literal["admin", "member", "viewer"] = "member"


class OrgInviteOut(BaseModel):
    id: UUID
    org_id: UUID
    email: str
    role: str
    token: str
    expires_at: datetime
    accepted_at: Optional[datetime]
    created_at: datetime

    model_config = {"from_attributes": True}


class OrgCreditsTransferRequest(BaseModel):
    """Transfer credits between personal account and org pool."""
    amount: int = Field(ge=1, le=100_000)
    direction: Literal["to_org", "from_org"]


class OrgUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=255)
    description: Optional[str] = None
    avatar_url: Optional[str] = None


# ─── Task Pipelines ───────────────────────────────────────────────────────

class PipelineStepCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    task_type: str
    execution_mode: Literal["ai", "human"] = "ai"
    task_config: dict = Field(default_factory=dict)
    input_mapping: Optional[dict] = None
    # Condition branches
    condition: Optional[str] = None          # JSONPath expression — step runs only if truthy
    next_on_pass: Optional[int] = None       # step_order to jump to on success (None = next)
    next_on_fail: Optional[int] = None       # step_order to jump to on failure (-1 = fail pipeline)
    # Auto-retry on failure (0 = no retry)
    max_retries: int = Field(0, ge=0, le=5, description="Auto-retry up to this many times on step failure")


class PipelineCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    description: Optional[str] = None
    org_id: Optional[UUID] = None
    steps: list[PipelineStepCreate] = Field(min_length=1, max_length=20)


class PipelineStepOut(BaseModel):
    id: UUID
    pipeline_id: UUID
    step_order: int
    name: str
    task_type: str
    execution_mode: str
    task_config: dict
    input_mapping: Optional[dict]
    condition: Optional[str] = None
    next_on_pass: Optional[int] = None
    next_on_fail: Optional[int] = None
    max_retries: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class PipelineOut(BaseModel):
    id: UUID
    user_id: UUID
    org_id: Optional[UUID]
    name: str
    description: Optional[str]
    is_active: bool
    step_count: int = 0
    run_count: int = 0
    created_at: datetime
    updated_at: Optional[datetime]

    model_config = {"from_attributes": True}


class PipelineDetailOut(PipelineOut):
    steps: list[PipelineStepOut] = []


class PipelineRunRequest(BaseModel):
    input: dict = Field(default_factory=dict)


class PipelineStepRunOut(BaseModel):
    id: UUID
    step_order: int
    task_id: Optional[UUID]
    status: str
    input: Optional[dict]
    output: Optional[dict]
    retry_count: int = 0
    started_at: Optional[datetime]
    completed_at: Optional[datetime]

    model_config = {"from_attributes": True}


class PipelineRunOut(BaseModel):
    id: UUID
    pipeline_id: UUID
    user_id: UUID
    status: str
    input: dict
    output: Optional[dict]
    current_step: int
    error: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    step_runs: list[PipelineStepRunOut] = []

    model_config = {"from_attributes": True}


class PaginatedPipelines(BaseModel):
    items: list[PipelineOut]
    total: int
    page: int
    page_size: int


class PaginatedPipelineRuns(BaseModel):
    items: list[PipelineRunOut]
    total: int
    page: int
    page_size: int


# ─── Worker Certifications ─────────────────────────────────────────────────

class CertificationQuestionOut(BaseModel):
    id: UUID
    question: str
    question_type: str
    options: Optional[list[dict]]
    explanation: Optional[str]   # only shown after answering
    points: int
    order_index: int

    model_config = {"from_attributes": True}


class CertificationOut(BaseModel):
    id: UUID
    task_type: str
    name: str
    description: Optional[str]
    passing_score: int
    badge_icon: Optional[str]
    question_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class CertificationDetailOut(CertificationOut):
    questions: list[CertificationQuestionOut] = []


class CertAttemptAnswer(BaseModel):
    question_id: UUID
    answer: Union[str, list[str]]


class CertAttemptRequest(BaseModel):
    answers: list[CertAttemptAnswer]


class CertAttemptResult(BaseModel):
    score: int           # % achieved
    passed: bool
    total_points: int
    earned_points: int
    question_count: int
    correct_count: int
    details: list[dict]  # per-question: {question_id, correct, earned, explanation}


class WorkerCertificationOut(BaseModel):
    id: UUID
    cert_id: UUID
    task_type: str
    cert_name: str
    badge_icon: Optional[str] = None  # emoji or icon name from CertificationDB
    score: int
    passed: bool
    attempt_count: int
    best_score: int
    certified_at: Optional[datetime]
    last_attempt_at: Optional[datetime]

    model_config = {"from_attributes": True}


# ─── Requester Analytics ───────────────────────────────────────────────────

class RequesterOverviewOut(BaseModel):
    total_tasks: int
    tasks_completed: int
    tasks_pending: int
    tasks_failed: int
    total_credits_spent: int
    credits_spent_30d: int = 0        # Credits spent in the last 30 days
    avg_completion_time_minutes: Optional[float]
    workers_used: int = 0             # Distinct workers who've submitted to requester's tasks
    tasks_by_type: dict[str, int]
    tasks_by_status: dict[str, int]
    tasks_last_30_days: list[dict]  # [{date, count}]


class OrgAnalyticsOut(BaseModel):
    org_id: UUID
    org_name: str
    total_tasks: int
    tasks_completed: int
    credits_spent: int
    member_activity: list[dict]  # [{user_id, name, tasks_created, credits_used}]
    tasks_by_type: dict[str, int]
    tasks_last_30_days: list[dict]


class CostBreakdownOut(BaseModel):
    total_credits_spent: int
    by_type: dict[str, int]
    by_execution_mode: dict[str, int]
    by_month: list[dict]  # [{month, credits}]
    top_task_types: list[dict]  # [{type, credits, count}]


# ─── Health ───────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    workers_online: int


# ─── Task Template Marketplace ────────────────────────────────────────────────

class TemplateCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    task_type: str
    execution_mode: str = "ai"
    category: Optional[str] = None
    tags: Optional[list[str]] = None
    task_config: dict = {}
    example_input: Optional[dict] = None
    is_public: bool = True


class TemplateOut(BaseModel):
    id: UUID
    creator_id: Optional[UUID]
    name: str
    description: Optional[str]
    task_type: str
    execution_mode: str
    category: Optional[str]
    tags: Optional[list]
    task_config: dict
    example_input: Optional[dict]
    is_public: bool
    is_featured: bool
    use_count: int
    rating_sum: int
    rating_count: int
    avg_rating: Optional[float] = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @property
    def _avg_rating(self) -> Optional[float]:
        if self.rating_count == 0:
            return None
        return round(self.rating_sum / self.rating_count, 1)


class PaginatedTemplates(BaseModel):
    items: list[TemplateOut]
    total: int
    page: int
    page_size: int


class TemplateRateRequest(BaseModel):
    rating: int  # 1–5


class TemplateRateResponse(BaseModel):
    template_id: UUID
    your_rating: int
    new_avg: Optional[float]
    total_ratings: int


# ─── Public Worker Profiles ────────────────────────────────────────────────

class PublicProfileSkill(BaseModel):
    task_type: str
    proficiency_level: int  # 1–5
    tasks_completed: int
    avg_accuracy: Optional[float]
    certified: bool

    model_config = {"from_attributes": True}


class PublicProfileBadge(BaseModel):
    badge_slug: str
    badge_name: str
    badge_description: Optional[str]
    badge_icon: Optional[str] = None  # emoji icon for the badge
    earned_at: datetime

    model_config = {"from_attributes": True}


class PublicWorkerProfileOut(BaseModel):
    """Publicly visible worker profile data."""
    id: UUID
    name: Optional[str]
    bio: Optional[str]
    avatar_url: Optional[str]
    location: Optional[str] = None
    website_url: Optional[str] = None
    role: str
    worker_level: int
    worker_xp: int
    worker_tasks_completed: int
    worker_accuracy: Optional[float]
    worker_reliability: Optional[float]
    reputation_score: float
    worker_streak_days: int
    avg_feedback_score: Optional[float] = None  # 1.0–5.0 star average from ratings
    total_ratings_received: int = 0              # total rating count
    skills: list[PublicProfileSkill]
    badges: list[PublicProfileBadge]
    member_since: datetime   # created_at

    model_config = {"from_attributes": True}


class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=255)
    bio: Optional[str] = Field(None, max_length=500)
    avatar_url: Optional[str] = Field(None, max_length=512)
    profile_public: Optional[bool] = None
    location: Optional[str] = Field(None, max_length=128)
    website_url: Optional[str] = Field(None, max_length=512)


# ─── Two-Factor Authentication ────────────────────────────────────────────

class TwoFASetupResponse(BaseModel):
    """TOTP setup — contains the provisioning URI for a QR code."""
    totp_uri: str       # otpauth:// URI for QR rendering
    secret: str         # raw base32 secret (show to user as backup)
    issuer: str


class TwoFAEnableRequest(BaseModel):
    """Verify a TOTP code and enable 2FA, receiving backup codes."""
    code: str = Field(min_length=6, max_length=6)


class TwoFAEnableResponse(BaseModel):
    backup_codes: list[str]   # 8 one-time backup codes (plaintext, store once)


class TwoFAVerifyRequest(BaseModel):
    """Provide TOTP code (or backup code) after password login."""
    pending_token: str
    code: str = Field(min_length=6, max_length=10)   # 6 TOTP or 8-char backup


class TwoFADisableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class TwoFAStatusResponse(BaseModel):
    enabled: bool
    backup_codes_remaining: int


class LoginWith2FAResponse(BaseModel):
    """Returned when 2FA is required during login."""
    requires_2fa: bool = True
    pending_token: str   # short-lived JWT to exchange after TOTP verify
    expires_in: int = 300   # seconds


# ─── Saved Searches ────────────────────────────────────────────────────────

class SavedSearchFilters(BaseModel):
    """Filters a worker can save for re-use or task alerts."""
    q: Optional[str] = None
    task_type: Optional[str] = None
    priority: Optional[str] = None
    min_reward: Optional[int] = None
    max_reward: Optional[int] = None


class SavedSearchCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    filters: SavedSearchFilters
    alert_enabled: bool = True
    alert_frequency: Literal["instant", "daily", "weekly"] = "instant"


class SavedSearchUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    filters: Optional[SavedSearchFilters] = None
    alert_enabled: Optional[bool] = None
    alert_frequency: Optional[Literal["instant", "daily", "weekly"]] = None


class SavedSearchOut(BaseModel):
    id: UUID
    name: str
    filters: dict
    alert_enabled: bool
    alert_frequency: str
    last_notified_at: Optional[datetime]
    match_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── API Key Usage Analytics ───────────────────────────────────────────────

class ApiKeyUsageDayOut(BaseModel):
    date: str          # "YYYY-MM-DD"
    requests: int
    errors: int
    credits_used: int
    avg_response_ms: Optional[float]


class ApiKeyUsageEndpointOut(BaseModel):
    endpoint: str
    method: str
    requests: int
    errors: int
    avg_response_ms: Optional[float]


class ApiKeyUsageDetailOut(BaseModel):
    key_id: UUID
    key_name: str
    key_prefix: str
    total_requests: int
    total_errors: int
    total_credits_used: int
    last_used_at: Optional[datetime]
    daily: list[ApiKeyUsageDayOut]
    top_endpoints: list[ApiKeyUsageEndpointOut]

    model_config = {"from_attributes": True}


class ApiKeyUsageOverviewOut(BaseModel):
    total_requests: int
    total_errors: int
    total_credits_used: int
    keys: list[dict]  # per-key summary


# ─── Skill Quiz ────────────────────────────────────────────────────────────

class SkillQuizQuestionOut(BaseModel):
    id: UUID
    question: str
    options: list[str]
    difficulty: int  # 1-3
    # correct_index is NOT exposed in the question fetch — only in results


class SkillQuizSubmitRequest(BaseModel):
    answers: list[int]        # index of chosen option per question
    question_ids: list[str] = []  # UUIDs of questions in the same order as answers


class SkillQuizResultOut(BaseModel):
    score: int           # number correct
    total: int
    passed: bool
    proficiency_level: int  # 1-5 set on their WorkerSkillDB
    skill_category: str
    questions: list[dict]   # with correct_index + explanation revealed
    credits_earned: int      # bonus credits for passing


class SkillQuizAttemptOut(BaseModel):
    id: UUID
    skill_category: str
    score: int
    total: int
    passed: bool
    proficiency_level: int
    created_at: datetime

    model_config = {"from_attributes": True}


# ─── Webhook Endpoint schemas ──────────────────────────────────────────────

class WebhookEndpointCreate(BaseModel):
    url: str
    description: Optional[str] = None
    events: Optional[list[str]] = None  # None = all events


class WebhookEndpointUpdate(BaseModel):
    url: Optional[str] = None
    description: Optional[str] = None
    events: Optional[list[str]] = None
    is_active: Optional[bool] = None


class WebhookEndpointOut(BaseModel):
    id: UUID
    url: str
    description: Optional[str]
    events: Optional[list[str]]
    is_active: bool
    delivery_count: int
    failure_count: int
    last_triggered_at: Optional[datetime]
    last_failure_at: Optional[datetime]
    created_at: datetime
    # Secret is only returned on creation (not subsequent fetches)
    secret: Optional[str] = None

    model_config = {"from_attributes": True}

# ─── Task Tags schemas ─────────────────────────────────────────────────────

class TaskTagsUpdate(BaseModel):
    """Replace task tags entirely."""
    tags: list[str] = Field(default_factory=list, max_length=20)


class TagStats(BaseModel):
    tag: str
    count: int


# ─── Requester Onboarding schemas ─────────────────────────────────────────

REQUESTER_ONBOARDING_STEPS = [
    "welcome",
    "create_task",
    "view_results",
    "set_webhook",
    "invite_team",
]

REQUESTER_STEP_META = {
    "welcome": {
        "title": "Complete your profile",
        "description": "Add a display name and learn your way around the dashboard.",
        "cta": "Go to profile",
        "cta_url": "/dashboard/profile",
        "icon": "👤",
    },
    "create_task": {
        "title": "Create your first task",
        "description": "Submit an AI-powered or human task to see CrowdSorcerer in action.",
        "cta": "Create a task",
        "cta_url": "/dashboard/tasks/new",
        "icon": "🚀",
    },
    "view_results": {
        "title": "View task results",
        "description": "Open a completed task to explore the rich result viewer.",
        "cta": "View tasks",
        "cta_url": "/dashboard/tasks",
        "icon": "📊",
    },
    "set_webhook": {
        "title": "Register a webhook",
        "description": "Connect your app to receive real-time task completion events.",
        "cta": "Set up webhook",
        "cta_url": "/dashboard/webhooks",
        "icon": "🔗",
    },
    "invite_team": {
        "title": "Invite a team member",
        "description": "Bring colleagues in to collaborate on tasks and share credits.",
        "cta": "Invite team",
        "cta_url": "/dashboard/team",
        "icon": "👥",
    },
}


class RequesterOnboardingStepOut(BaseModel):
    key: str
    title: str
    description: str
    cta: str
    cta_url: str
    icon: str
    completed: bool


class RequesterOnboardingStatusOut(BaseModel):
    steps: list[RequesterOnboardingStepOut]
    completed_count: int
    total_steps: int
    all_complete: bool
    bonus_claimed: bool
    bonus_credits: int = 200


# ─── Webhook Payload Templates ─────────────────────────────────────────────

class WebhookPayloadTemplate(BaseModel):
    event_type: str
    template: str  # JSON string with {{field}} placeholders
    description: Optional[str] = None


class WebhookPayloadTemplateOut(BaseModel):
    id: int
    user_id: str
    event_type: str
    template: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ─── Requester Saved Templates ─────────────────────────────────────────────

class RequesterTemplateCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=1000)
    task_type: str = Field(min_length=1, max_length=64)
    task_input: dict[str, Any] = Field(default_factory=dict)
    task_config: dict[str, Any] = Field(default_factory=dict)   # priority, tags, etc.
    icon: Optional[str] = Field(None, max_length=8)


class RequesterTemplateUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = Field(None, max_length=1000)
    task_input: Optional[dict[str, Any]] = None
    task_config: Optional[dict[str, Any]] = None
    icon: Optional[str] = Field(None, max_length=8)


class RequesterTemplateOut(BaseModel):
    id: UUID
    user_id: UUID
    name: str
    description: Optional[str]
    task_type: str
    task_input: dict
    task_config: dict
    icon: Optional[str]
    use_count: int
    # Marketplace fields
    is_public: bool = False
    marketplace_title: Optional[str] = None
    marketplace_description: Optional[str] = None
    marketplace_tags: list[str] = []
    import_count: int = 0
    published_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class RequesterTemplateListOut(BaseModel):
    templates: list[RequesterTemplateOut]
    total: int


# ─── Template Marketplace ─────────────────────────────────────────────────────

class TemplatePublishRequest(BaseModel):
    marketplace_title: Optional[str] = Field(None, max_length=255)
    marketplace_description: Optional[str] = Field(None, max_length=2000)
    marketplace_tags: list[str] = Field(default_factory=list, max_length=10)


class MarketplaceTemplateOut(BaseModel):
    """Public view of a template in the marketplace."""
    id: UUID
    name: str
    task_type: str
    icon: Optional[str]
    use_count: int
    import_count: int
    marketplace_title: Optional[str]
    marketplace_description: Optional[str]
    marketplace_tags: list[str]
    published_at: Optional[datetime]
    # Author info (stripped — just display name)
    author_name: Optional[str] = None
    author_reputation: Optional[float] = None

    model_config = {"from_attributes": True}


class MarketplaceTemplateListOut(BaseModel):
    templates: list[MarketplaceTemplateOut]
    total: int
    page: int
    page_size: int
    has_next: bool


# ─── Bulk Worker Invites ───────────────────────────────────────────────────

class BulkInviteRequest(BaseModel):
    worker_ids: list[UUID] = Field(min_length=1, max_length=50)
    message: Optional[str] = Field(None, max_length=500)


class BulkInviteResult(BaseModel):
    invited: int
    skipped: int
    invite_ids: list[str]


# ── Worker Teams (migration 0033) ─────────────────────────────────────────────

class WorkerTeamMemberOut(BaseModel):
    user_id: str
    name: str
    role: str        # owner | member
    joined_at: str
    tasks_completed: int = 0
    xp: int = 0
    level: int = 1

    model_config = {"from_attributes": True}


class WorkerTeamInviteOut(BaseModel):
    id: str
    team_id: str
    team_name: str
    invitee_id: str
    invitee_name: Optional[str] = None
    invited_by: str
    inviter_name: str
    status: str
    message: Optional[str] = None
    created_at: str
    expires_at: Optional[str] = None

    model_config = {"from_attributes": True}


class WorkerTeamOut(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    avatar_emoji: str = "👥"
    created_by: str
    member_count: int = 0
    created_at: str
    updated_at: str
    my_role: Optional[str] = None   # owner | member | None (not a member)

    model_config = {"from_attributes": True}


class WorkerTeamDetailOut(WorkerTeamOut):
    members: list[WorkerTeamMemberOut] = []
    pending_invites: list[WorkerTeamInviteOut] = []


class WorkerTeamCreateRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    description: Optional[str] = Field(None, max_length=500)
    avatar_emoji: Optional[str] = Field(None, max_length=8)


class WorkerTeamInviteRequest(BaseModel):
    username: str = Field(..., description="Username or email of the worker to invite")
    message: Optional[str] = Field(None, max_length=500)


class PaginatedWorkerTeams(BaseModel):
    items: list[WorkerTeamOut]
    total: int
    page: int
    page_size: int
