"""SQLAlchemy ORM models."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Integer, BigInteger, Boolean, DateTime, Float,
    Text, ForeignKey, Enum as SAEnum, JSON, Date, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship, backref

from core.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class UserDB(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=True)
    password_hash = Column(String(255), nullable=True)
    plan = Column(
        SAEnum("free", "starter", "pro", "enterprise", name="plan_enum"),
        default="free",
        nullable=False,
    )
    # Role: requester = posts tasks; worker = completes tasks; both = can do either
    role = Column(
        SAEnum("requester", "worker", "both", name="user_role_enum"),
        default="requester",
        nullable=False,
    )
    credits = Column(Integer, default=100, nullable=False)
    stripe_customer_id = Column(String(255), nullable=True, unique=True)
    is_active = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)

    # Referral system
    referral_code = Column(String(16), unique=True, nullable=True, index=True)  # User's own code

    # Active org context (which org this user is currently "acting as")
    active_org_id = Column(UUID(as_uuid=True),
                           ForeignKey("organizations.id", ondelete="SET NULL"),
                           nullable=True)
    # Pending credits = earned credits not yet confirmed (paid after first task completion)
    credits_pending = Column(Integer, default=0, nullable=False)

    # Worker gamification
    worker_xp = Column(Integer, default=0, nullable=False)
    worker_level = Column(Integer, default=1, nullable=False)
    worker_accuracy = Column(Float, nullable=True)       # 0.0–1.0
    worker_reliability = Column(Float, nullable=True)    # 0.0–1.0
    worker_tasks_completed = Column(Integer, default=0, nullable=False)
    worker_streak_days = Column(Integer, default=0, nullable=False)
    worker_last_active_date = Column(DateTime(timezone=True), nullable=True)

    # Worker reputation & moderation
    reputation_score = Column(Float, default=50.0, nullable=False)  # 0.0–100.0
    strike_count = Column(Integer, default=0, nullable=False)
    is_banned = Column(Boolean, default=False, nullable=False)
    ban_reason = Column(Text, nullable=True)
    ban_expires_at = Column(DateTime(timezone=True), nullable=True)  # None = permanent ban

    # Public profile fields
    bio = Column(Text, nullable=True)
    avatar_url = Column(String(512), nullable=True)
    profile_public = Column(Boolean, default=True, nullable=False)

    # Credit burn-rate alerts
    credit_alert_threshold = Column(Integer, nullable=True)          # fire alert when credits drop below this
    credit_alert_fired = Column(Boolean, default=False, nullable=False)  # reset when topped up

    # TOTP 2FA
    totp_secret = Column(String(64), nullable=True)
    totp_enabled = Column(Boolean, default=False, nullable=False)
    totp_backup_codes = Column(JSON, nullable=True)          # list[str] hashed codes
    totp_pending_token = Column(String(512), nullable=True)  # short-lived pre-2FA JWT

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    tasks = relationship("TaskDB", back_populates="user", lazy="dynamic")
    api_keys = relationship("ApiKeyDB", back_populates="user", lazy="dynamic")
    transactions = relationship("CreditTransactionDB", back_populates="user", lazy="dynamic")
    assignments = relationship("TaskAssignmentDB", back_populates="worker", lazy="dynamic",
                               foreign_keys="TaskAssignmentDB.worker_id")
    badges = relationship("WorkerBadgeDB", back_populates="user", lazy="dynamic")
    saved_searches = relationship("SavedSearchDB", back_populates="user", lazy="dynamic")


class ApiKeyDB(Base):
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    key_hash = Column(String(64), unique=True, nullable=False, index=True)
    key_prefix = Column(String(16), nullable=False)
    scopes = Column(JSON, default=list, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    request_count = Column(Integer, default=0, nullable=False)
    total_credits_used = Column(Integer, default=0, nullable=False)
    # Per-key overrides for rate limiting (None = use plan default)
    rate_limit_rpm = Column(Integer, nullable=True)    # requests per minute
    rate_limit_daily = Column(Integer, nullable=True)  # requests per day
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("UserDB", back_populates="api_keys")
    rate_buckets = relationship("ApiKeyRateBucketDB", back_populates="api_key",
                                cascade="all, delete-orphan")


class ApiKeyRateBucketDB(Base):
    """Per-API-key sliding-window rate-limit counters."""
    __tablename__ = "api_key_rate_buckets"
    __table_args__ = (
        UniqueConstraint("api_key_id", "window_key", name="uq_api_key_rate_bucket"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_key_id = Column(UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    window_key = Column(String(64), nullable=False)   # e.g. "rpm:2026-03-23T14:05" or "daily:2026-03-23"
    count = Column(Integer, default=0, nullable=False)
    reset_at = Column(DateTime(timezone=True), nullable=False)

    api_key = relationship("ApiKeyDB", back_populates="rate_buckets")


class TaskDB(Base):
    __tablename__ = "tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    type = Column(
        SAEnum(
            # AI-powered task types (executed by RebaseKit APIs)
            "web_research", "entity_lookup", "document_parse", "data_transform",
            "llm_generate", "screenshot", "audio_transcribe", "pii_detect",
            "code_execute", "web_intel",
            # Human task types (completed by workers in the marketplace)
            "label_image", "label_text", "rate_quality",
            "verify_fact", "moderate_content", "compare_rank",
            "answer_question", "transcription_review",
            name="task_type_enum",
        ),
        nullable=False,
    )
    status = Column(
        SAEnum(
            "pending", "queued", "running",  # AI task lifecycle
            "open",                           # Human task: available in marketplace
            "assigned",                       # Human task: claimed by a worker
            "completed", "failed", "cancelled",
            name="task_status_enum",
        ),
        default="pending",
        nullable=False,
    )
    priority = Column(
        SAEnum("low", "normal", "high", "urgent", name="task_priority_enum"),
        default="normal",
        nullable=False,
    )
    # Execution mode determines whether task is run by AI or sent to human workers
    execution_mode = Column(
        SAEnum("ai", "human", name="execution_mode_enum"),
        default="ai",
        nullable=False,
    )

    input = Column(JSON, nullable=False)
    output = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    credits_used = Column(Integer, nullable=True)
    duration_ms = Column(BigInteger, nullable=True)
    webhook_url = Column(String(2048), nullable=True)
    # Subscribed webhook events. NULL/empty → default ["task.completed"]
    webhook_events = Column(JSON, nullable=True)
    metadata = Column(JSON, nullable=True)

    # Human task fields
    worker_reward_credits = Column(Integer, nullable=True)      # Credits paid to each worker
    assignments_required = Column(Integer, default=1, nullable=False)  # Workers needed (for consensus)
    assignments_completed = Column(Integer, default=0, nullable=False)
    claim_timeout_minutes = Column(Integer, default=30, nullable=False)
    task_instructions = Column(Text, nullable=True)             # Extra guidance for workers

    # Quality control fields
    is_gold_standard = Column(Boolean, default=False, nullable=False)  # Hidden QC task
    gold_answer = Column(JSON, nullable=True)                          # Expected answer for QC
    min_reputation_score = Column(Float, nullable=True)                # Only workers with rep >= this can claim
    min_skill_level = Column(Integer, nullable=True)                    # 1–5; require worker proficiency >= this

    # Dispute / consensus fields (for multi-worker human tasks)
    # Strategies: any_first | majority_vote | unanimous | requester_review
    consensus_strategy = Column(String(32), default="any_first", nullable=False)
    dispute_status = Column(String(32), nullable=True)  # None | disputed | resolved
    winning_assignment_id = Column(UUID(as_uuid=True),
                                   ForeignKey("task_assignments.id", ondelete="SET NULL"),
                                   nullable=True)
    mediator_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
                         nullable=True)  # Admin mediator assigned to dispute

    # Org scoping (optional — tasks can belong to an org's shared pool)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"),
                    nullable=True, index=True)

    # Task labels / tags — JSON array of strings (e.g. ["billing","v2","prod"])
    tags = Column(JSON, nullable=True)

    # Deferred execution — if set and in the future, sweeper holds until time arrives
    scheduled_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("UserDB", back_populates="tasks")
    assignments = relationship("TaskAssignmentDB", back_populates="task", lazy="dynamic",
                               foreign_keys="TaskAssignmentDB.task_id")


class TaskAssignmentDB(Base):
    """Links a worker to a specific human task they are working on or completed."""
    __tablename__ = "task_assignments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    worker_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    status = Column(
        SAEnum("active", "submitted", "approved", "rejected", "released", "timed_out",
               name="assignment_status_enum"),
        default="active",
        nullable=False,
    )
    response = Column(JSON, nullable=True)          # Worker's answer/completion data
    worker_note = Column(Text, nullable=True)       # Optional note from worker
    earnings_credits = Column(Integer, default=0, nullable=False)  # Credits earned
    xp_earned = Column(Integer, default=0, nullable=False)

    claimed_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    released_at = Column(DateTime(timezone=True), nullable=True)
    timeout_at = Column(DateTime(timezone=True), nullable=True)  # When the claim expires

    task = relationship("TaskDB", back_populates="assignments", foreign_keys=[task_id])
    worker = relationship("UserDB", back_populates="assignments", foreign_keys=[worker_id])


class CreditTransactionDB(Base):
    __tablename__ = "credit_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    amount = Column(Integer, nullable=False)  # positive = credit, negative = charge
    type = Column(
        SAEnum("charge", "credit", "refund", "earning", name="transaction_type_enum"),
        nullable=False,
    )
    description = Column(String(512), nullable=False)
    stripe_payment_intent = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("UserDB", back_populates="transactions")


class WorkerBadgeDB(Base):
    """Badges/achievements earned by workers."""
    __tablename__ = "worker_badges"
    __table_args__ = (
        UniqueConstraint("user_id", "badge_id", name="uq_worker_badge"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    badge_id = Column(String(64), nullable=False)   # e.g. "first_task", "streak_7"
    earned_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("UserDB", back_populates="badges")


class DailyChallengeDB(Base):
    """One challenge per day — a special task type with bonus rewards."""
    __tablename__ = "daily_challenges"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    challenge_date = Column(Date, unique=True, nullable=False, index=True)
    task_type = Column(String(64), nullable=False)       # The required human task type
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    bonus_xp = Column(Integer, default=25, nullable=False)
    bonus_credits = Column(Integer, default=5, nullable=False)
    target_count = Column(Integer, default=3, nullable=False)  # Tasks needed to claim reward
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class DailyChallengeProgressDB(Base):
    """Tracks a worker's progress on today's daily challenge."""
    __tablename__ = "daily_challenge_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "challenge_id", name="uq_challenge_progress"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    challenge_id = Column(UUID(as_uuid=True), ForeignKey("daily_challenges.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    tasks_completed = Column(Integer, default=0, nullable=False)
    bonus_claimed = Column(Boolean, default=False, nullable=False)
    bonus_claimed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WebhookLogDB(Base):
    """Log of webhook delivery attempts for a task."""
    __tablename__ = "webhook_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    url = Column(String(2048), nullable=False)
    event_type = Column(String(64), nullable=True, default="task.completed")  # e.g. task.created
    attempt = Column(Integer, default=1, nullable=False)  # 1-indexed
    status_code = Column(Integer, nullable=True)          # HTTP status if response received
    success = Column(Boolean, default=False, nullable=False)
    error = Column(Text, nullable=True)                   # Error message if failed
    duration_ms = Column(Integer, nullable=True)
    retry_of = Column(UUID(as_uuid=True), nullable=True)  # original log ID if this is a manual retry
    is_manual_retry = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class PayoutRequestDB(Base):
    """Worker payout / withdrawal request."""
    __tablename__ = "payout_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    credits_requested = Column(Integer, nullable=False)      # Credits to cash out
    usd_amount = Column(Float, nullable=False)               # Equivalent USD (credits / 100)
    status = Column(
        SAEnum("pending", "processing", "paid", "rejected", name="payout_status_enum"),
        default="pending",
        nullable=False,
        index=True,
    )
    payout_method = Column(
        SAEnum("paypal", "bank_transfer", "crypto", name="payout_method_enum"),
        nullable=False,
    )
    payout_details = Column(JSON, nullable=False)            # e.g. {"email": "..."} for PayPal
    admin_note = Column(Text, nullable=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    worker = relationship("UserDB", backref="payout_requests", foreign_keys=[worker_id])


class ReferralDB(Base):
    """Referral tracking — when a user signs up via a referral code."""
    __tablename__ = "referrals"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    referrer_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    referred_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                         nullable=False, unique=True, index=True)
    # Bonus tracking
    referrer_bonus_credits = Column(Integer, default=50, nullable=False)
    referred_bonus_credits = Column(Integer, default=50, nullable=False)  # Extra over base 100
    bonus_paid = Column(Boolean, default=False, nullable=False)           # Paid after first task
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    referrer = relationship("UserDB", backref="referrals_made",
                            foreign_keys=[referrer_id])
    referred = relationship("UserDB", backref="referral_from",
                            foreign_keys=[referred_id])


class WorkerSkillDB(Base):
    """Per-task-type skill profile for a worker. Updated on each approved/rejected assignment."""
    __tablename__ = "worker_skills"
    __table_args__ = (
        UniqueConstraint("worker_id", "task_type", name="uq_worker_skill"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    task_type = Column(String(64), nullable=False, index=True)
    tasks_completed = Column(Integer, default=0, nullable=False)
    tasks_approved = Column(Integer, default=0, nullable=False)
    tasks_rejected = Column(Integer, default=0, nullable=False)
    accuracy = Column(Float, nullable=True)               # approved / (approved + rejected)
    avg_response_minutes = Column(Float, nullable=True)   # avg time from claim to submit
    credits_earned = Column(Integer, default=0, nullable=False)
    proficiency_level = Column(Integer, default=1, nullable=False)  # 1–5
    match_weight = Column(Float, default=1.0, nullable=True)         # routing weight modifier
    last_task_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    worker = relationship("UserDB", backref="skills", foreign_keys=[worker_id])


class OrganizationDB(Base):
    """An organization / team that groups users and shares a credits pool."""
    __tablename__ = "organizations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    slug = Column(String(64), unique=True, nullable=False, index=True)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    credits = Column(Integer, default=0, nullable=False)
    plan = Column(String(32), default="free", nullable=False)
    description = Column(Text, nullable=True)
    avatar_url = Column(String(2048), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    owner = relationship("UserDB", backref="owned_orgs", foreign_keys=[owner_id])
    members = relationship("OrgMemberDB", back_populates="org", lazy="dynamic")
    invites = relationship("OrgInviteDB", back_populates="org", lazy="dynamic")


class OrgMemberDB(Base):
    """Membership of a user in an organization."""
    __tablename__ = "org_members"
    __table_args__ = (
        UniqueConstraint("org_id", "user_id", name="uq_org_member"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    role = Column(String(32), default="member", nullable=False)  # owner | admin | member | viewer
    joined_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    org = relationship("OrganizationDB", back_populates="members")
    user = relationship("UserDB", backref="org_memberships", foreign_keys=[user_id])


class OrgInviteDB(Base):
    """Pending invitation to join an organization."""
    __tablename__ = "org_invites"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"),
                    nullable=False, index=True)
    email = Column(String(255), nullable=False)
    role = Column(String(32), default="member", nullable=False)
    token = Column(String(64), unique=True, nullable=False, index=True)
    invited_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
                        nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    org = relationship("OrganizationDB", back_populates="invites")
    inviter = relationship("UserDB", backref="org_invites_sent", foreign_keys=[invited_by])


class NotificationDB(Base):
    """In-app notification for a user."""
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    # Type: task_completed, task_failed, submission_received, submission_approved,
    #       submission_rejected, referral_bonus, payout_processing, payout_paid,
    #       payout_rejected, challenge_completed, badge_earned
    type = Column(String(64), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    link = Column(String(512), nullable=True)   # optional URL to navigate to
    is_read = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("UserDB", backref="notifications")


# ─── Task Pipelines ───────────────────────────────────────────────────────────

class TaskPipelineDB(Base):
    """A reusable pipeline definition — an ordered chain of task steps."""
    __tablename__ = "task_pipelines"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL"),
                    nullable=True, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user = relationship("UserDB", backref="pipelines")
    steps = relationship("TaskPipelineStepDB", back_populates="pipeline",
                         order_by="TaskPipelineStepDB.step_order", cascade="all, delete-orphan")
    runs = relationship("TaskPipelineRunDB", back_populates="pipeline", lazy="dynamic")


class TaskPipelineStepDB(Base):
    """One step within a pipeline definition."""
    __tablename__ = "task_pipeline_steps"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pipeline_id = Column(UUID(as_uuid=True), ForeignKey("task_pipelines.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    step_order = Column(Integer, nullable=False)          # 0-based ordering
    name = Column(String(255), nullable=False)
    task_type = Column(String(64), nullable=False)         # e.g. "llm_generate", "rate_quality"
    execution_mode = Column(String(16), default="ai", nullable=False)  # ai | human
    # Static config merged with dynamic input; {key: value} where value can be
    # a template string like "{{prev.output.text}}" referencing prior step output
    task_config = Column(JSON, nullable=False, default=dict)
    # Input mapping: how to pull fields from pipeline run input or prior step output
    # e.g. {"prompt": "$.input.text", "context": "$.steps.0.output.summary"}
    input_mapping = Column(JSON, nullable=True)
    # Condition branches ─────────────────────────────────────────────────────
    # Optional JSONPath expression; if it evaluates to falsy, step is skipped.
    # e.g. "$.steps.0.output.score > 0.8" — runs only if prior step score > 0.8
    # Supported operators: ==, !=, >, >=, <, <= and path existence check (no op)
    condition = Column(Text, nullable=True)
    # Step index to go to on success (None = next sequential step)
    next_on_pass = Column(Integer, nullable=True)
    # Step index to go to on failure: -1 = fail pipeline (default), >= 0 = branch
    next_on_fail = Column(Integer, nullable=True)
    # Auto-retry on failure: 0 = no retry, N = retry up to N times before failing
    max_retries = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    pipeline = relationship("TaskPipelineDB", back_populates="steps")


class TaskPipelineRunDB(Base):
    """One execution of a pipeline — tracks state across all steps."""
    __tablename__ = "task_pipeline_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pipeline_id = Column(UUID(as_uuid=True), ForeignKey("task_pipelines.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    status = Column(
        SAEnum("pending", "running", "completed", "failed", "cancelled",
               name="pipeline_run_status_enum"),
        default="pending",
        nullable=False,
        index=True,
    )
    input = Column(JSON, nullable=False, default=dict)      # Initial input payload
    output = Column(JSON, nullable=True)                    # Final output from last step
    current_step = Column(Integer, default=0, nullable=False)  # which step index is active
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    pipeline = relationship("TaskPipelineDB", back_populates="runs")
    user = relationship("UserDB", backref="pipeline_runs")
    step_runs = relationship("TaskPipelineStepRunDB", back_populates="run",
                              order_by="TaskPipelineStepRunDB.step_order",
                              cascade="all, delete-orphan")


class TaskPipelineStepRunDB(Base):
    """Tracks one step's execution within a pipeline run."""
    __tablename__ = "task_pipeline_step_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(UUID(as_uuid=True), ForeignKey("task_pipeline_runs.id", ondelete="CASCADE"),
                    nullable=False, index=True)
    step_id = Column(UUID(as_uuid=True), ForeignKey("task_pipeline_steps.id", ondelete="CASCADE"),
                     nullable=False)
    step_order = Column(Integer, nullable=False)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"),
                     nullable=True)  # The actual Task created for this step
    status = Column(
        SAEnum("pending", "running", "completed", "failed", "skipped", "retrying",
               name="step_run_status_enum"),
        default="pending",
        nullable=False,
    )
    input = Column(JSON, nullable=True)    # Resolved input for this step
    output = Column(JSON, nullable=True)   # Output from the task
    retry_count = Column(Integer, default=0, nullable=False)  # How many retries so far
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    run = relationship("TaskPipelineRunDB", back_populates="step_runs")
    step = relationship("TaskPipelineStepDB")


# ─── Worker Certification ─────────────────────────────────────────────────────

class CertificationDB(Base):
    """A certification program for a specific task type."""
    __tablename__ = "certifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_type = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    passing_score = Column(Integer, default=70, nullable=False)  # % needed to pass
    badge_icon = Column(String(64), nullable=True)               # emoji or icon name
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    questions = relationship("CertificationQuestionDB", back_populates="certification",
                              cascade="all, delete-orphan")
    worker_certs = relationship("WorkerCertificationDB", back_populates="certification",
                                 lazy="dynamic")


class CertificationQuestionDB(Base):
    """One question in a certification test."""
    __tablename__ = "certification_questions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cert_id = Column(UUID(as_uuid=True), ForeignKey("certifications.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    question = Column(Text, nullable=False)
    question_type = Column(String(32), default="single_choice", nullable=False)
    # single_choice | multi_choice | text_match
    options = Column(JSON, nullable=True)           # [{"id": "a", "text": "..."}, ...]
    correct_answer = Column(JSON, nullable=False)   # "a" | ["a","c"] | "expected text"
    explanation = Column(Text, nullable=True)       # Shown after answering
    points = Column(Integer, default=10, nullable=False)
    order_index = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    certification = relationship("CertificationDB", back_populates="questions")


class WorkerCertificationDB(Base):
    """A worker's certification result for a specific task type."""
    __tablename__ = "worker_certifications"
    __table_args__ = (
        UniqueConstraint("worker_id", "cert_id", name="uq_worker_cert"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    cert_id = Column(UUID(as_uuid=True), ForeignKey("certifications.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    score = Column(Integer, default=0, nullable=False)    # % achieved
    passed = Column(Boolean, default=False, nullable=False)
    attempt_count = Column(Integer, default=0, nullable=False)
    best_score = Column(Integer, default=0, nullable=False)
    certified_at = Column(DateTime(timezone=True), nullable=True)  # when first passed
    last_attempt_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    worker = relationship("UserDB", backref="certifications")
    certification = relationship("CertificationDB", back_populates="worker_certs")


# ─── Task Template Marketplace ────────────────────────────────────────────────

class TaskTemplateDB(Base):
    """A user-created or system task template available in the marketplace."""
    __tablename__ = "task_templates_marketplace"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    creator_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=True)  # NULL = system template
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    task_type = Column(String(64), nullable=False, index=True)
    execution_mode = Column(
        SAEnum("ai", "human", name="template_exec_mode_enum"),
        default="ai",
        nullable=False,
    )
    category = Column(String(64), nullable=True, index=True)  # e.g. "data_labeling", "moderation"
    tags = Column(JSON, nullable=True)            # ["nlp", "image", ...]
    task_config = Column(JSON, nullable=False, default=dict)  # default task input fields
    example_input = Column(JSON, nullable=True)   # Example input shown in preview
    is_public = Column(Boolean, default=True, nullable=False)
    is_featured = Column(Boolean, default=False, nullable=False)
    use_count = Column(Integer, default=0, nullable=False)
    rating_sum = Column(Integer, default=0, nullable=False)
    rating_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    creator = relationship("UserDB", backref="marketplace_templates")


class TaskTemplateRatingDB(Base):
    """A user's rating (1–5 stars) of a marketplace template."""
    __tablename__ = "task_template_ratings"
    __table_args__ = (
        UniqueConstraint("template_id", "user_id", name="uq_template_rating"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    template_id = Column(UUID(as_uuid=True),
                         ForeignKey("task_templates_marketplace.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    rating = Column(Integer, nullable=False)  # 1–5
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


# ─── Rate Limit Quota Buckets ─────────────────────────────────────────────────

class RateLimitBucketDB(Base):
    """Per-user daily/monthly usage counters for rate limiting."""
    __tablename__ = "rate_limit_buckets"
    __table_args__ = (
        UniqueConstraint("user_id", "bucket_key", name="uq_rate_limit_bucket"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    bucket_key = Column(String(64), nullable=False)  # e.g. "tasks:2026-03-23"
    count = Column(Integer, default=0, nullable=False)
    reset_at = Column(DateTime(timezone=True), nullable=False)

    user = relationship("UserDB", backref="rate_limit_buckets")


# ─── Worker Reputation & Moderation ──────────────────────────────────────────

class WorkerStrikeDB(Base):
    """Moderation strike issued to a worker by an admin."""
    __tablename__ = "worker_strikes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    issued_by = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
                       nullable=True)
    # severity: warning | minor | major | critical
    severity = Column(String(16), default="minor", nullable=False)
    reason = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)  # False = pardoned
    expires_at = Column(DateTime(timezone=True), nullable=True)  # None = permanent
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    worker = relationship("UserDB", backref="strikes_received", foreign_keys=[worker_id])
    admin = relationship("UserDB", backref="strikes_issued", foreign_keys=[issued_by])


# ─── Pipeline Triggers ────────────────────────────────────────────────────────

class PipelineTriggerDB(Base):
    """A trigger that automatically fires a pipeline run on a schedule or via webhook."""
    __tablename__ = "pipeline_triggers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pipeline_id = Column(UUID(as_uuid=True), ForeignKey("task_pipelines.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    # type: schedule | webhook
    trigger_type = Column(String(16), nullable=False)
    name = Column(String(255), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    # Schedule fields (type=schedule)
    cron_expression = Column(String(64), nullable=True)   # e.g. "0 9 * * 1" = Mon 9am
    # Webhook fields (type=webhook)
    webhook_token = Column(String(64), unique=True, nullable=True, index=True)
    # Default input to pass to the pipeline run when trigger fires
    default_input = Column(JSON, nullable=True)
    # Tracking
    last_fired_at = Column(DateTime(timezone=True), nullable=True)
    next_fire_at = Column(DateTime(timezone=True), nullable=True)  # computed from cron
    run_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    pipeline = relationship("TaskPipelineDB", backref="triggers")
    user = relationship("UserDB", backref="pipeline_triggers")


# ─── A/B Experiments ──────────────────────────────────────────────────────────

class ABExperimentDB(Base):
    """A/B experiment to compare different task configurations."""
    __tablename__ = "ab_experiments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    hypothesis = Column(Text, nullable=True)
    status = Column(
        SAEnum("draft", "running", "paused", "completed", name="ab_exp_status_enum"),
        default="draft", nullable=False,
    )
    task_type = Column(String(32), nullable=True)   # filter: which task type this tests
    # Primary metric for determining winner
    primary_metric = Column(
        SAEnum("completion_rate", "accuracy", "avg_time", "credits_used",
               name="ab_metric_enum"),
        default="completion_rate", nullable=False,
    )
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    winner_variant_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    variants = relationship("ABVariantDB", back_populates="experiment", cascade="all, delete-orphan")
    user = relationship("UserDB", backref="ab_experiments")


class ABVariantDB(Base):
    """A variant (arm) within an A/B experiment."""
    __tablename__ = "ab_variants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id = Column(UUID(as_uuid=True), ForeignKey("ab_experiments.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    name = Column(String(64), nullable=False)          # e.g. "Control", "Variant A"
    description = Column(Text, nullable=True)
    traffic_pct = Column(Float, default=50.0, nullable=False)  # % of tasks routed here
    task_config = Column(JSON, nullable=True)           # config overrides (prompt, settings)
    is_control = Column(Boolean, default=False, nullable=False)
    # Rolling stats (updated on task completion)
    participant_count = Column(Integer, default=0, nullable=False)
    completion_count = Column(Integer, default=0, nullable=False)
    failure_count = Column(Integer, default=0, nullable=False)
    total_accuracy = Column(Float, default=0.0, nullable=False)   # sum for rolling average
    total_duration_ms = Column(BigInteger, default=0, nullable=False)
    total_credits_used = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    experiment = relationship("ABExperimentDB", back_populates="variants")


class ABParticipantDB(Base):
    """A task that was enrolled in an A/B experiment variant."""
    __tablename__ = "ab_participants"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    experiment_id = Column(UUID(as_uuid=True), ForeignKey("ab_experiments.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    variant_id = Column(UUID(as_uuid=True), ForeignKey("ab_variants.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"),
                     nullable=True, unique=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
                     nullable=True)
    assigned_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    outcome = Column(String(16), nullable=True)        # completed / failed / cancelled
    accuracy = Column(Float, nullable=True)
    duration_ms = Column(BigInteger, nullable=True)
    credits_used = Column(Integer, nullable=True)


# ─── Worker Onboarding ────────────────────────────────────────────────────────

class OnboardingProgressDB(Base):
    """Tracks a worker's progress through the onboarding flow."""
    __tablename__ = "onboarding_progress"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, unique=True, index=True)
    # Step completion flags
    step_profile = Column(Boolean, default=False, nullable=False)     # Set display name
    step_explore = Column(Boolean, default=False, nullable=False)     # Browse marketplace
    step_first_task = Column(Boolean, default=False, nullable=False)  # Complete first task
    step_skills = Column(Boolean, default=False, nullable=False)      # View skills page
    step_cert = Column(Boolean, default=False, nullable=False)        # Attempt any cert
    # State
    completed_at = Column(DateTime(timezone=True), nullable=True)
    skipped_at = Column(DateTime(timezone=True), nullable=True)
    bonus_claimed = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    user = relationship("UserDB", backref="onboarding_progress")


# ─── SLA Breaches ─────────────────────────────────────────────────────────────

class NotificationPreferencesDB(Base):
    """Per-user preferences for email and in-app notification delivery."""
    __tablename__ = "notification_preferences"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, unique=True, index=True)

    # ── Email notification toggles ──────────────────────────────────────────
    email_task_completed = Column(Boolean, default=True, nullable=False)
    email_task_failed = Column(Boolean, default=True, nullable=False)
    email_submission_received = Column(Boolean, default=True, nullable=False)
    email_worker_approved = Column(Boolean, default=True, nullable=False)
    email_payout_update = Column(Boolean, default=True, nullable=False)
    email_daily_challenge = Column(Boolean, default=False, nullable=False)  # opt-in only
    email_referral_bonus = Column(Boolean, default=True, nullable=False)
    email_sla_breach = Column(Boolean, default=True, nullable=False)

    # ── In-app notification toggles ─────────────────────────────────────────
    notif_task_events = Column(Boolean, default=True, nullable=False)   # completed, failed
    notif_submissions = Column(Boolean, default=True, nullable=False)   # received, approved, rejected
    notif_payouts = Column(Boolean, default=True, nullable=False)
    notif_gamification = Column(Boolean, default=True, nullable=False)  # badges, challenges, streaks
    notif_system = Column(Boolean, default=True, nullable=False)        # announcements, referrals

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user = relationship("UserDB", backref="notification_preferences", uselist=False)


class SLABreachDB(Base):
    """Logs tasks that exceeded their SLA (time-to-complete guarantee)."""
    __tablename__ = "sla_breaches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"),
                     nullable=False, unique=True, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
                     nullable=True)
    plan = Column(String(16), nullable=False)
    priority = Column(String(16), nullable=False, default="normal")
    sla_hours = Column(Float, nullable=False)          # target SLA in hours
    task_created_at = Column(DateTime(timezone=True), nullable=False)
    breach_at = Column(DateTime(timezone=True), nullable=False)       # when SLA was first exceeded
    resolved_at = Column(DateTime(timezone=True), nullable=True)      # when task finally completed
    credits_refunded = Column(Integer, default=0, nullable=False)     # partial refund on breach


# ─── Task Comments ────────────────────────────────────────────────────────────

class TaskCommentDB(Base):
    """Threaded comment/discussion on a task between requester and workers."""
    __tablename__ = "task_comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("task_comments.id", ondelete="CASCADE"),
                       nullable=True, index=True)  # None = top-level comment
    body = Column(Text, nullable=False)
    is_internal = Column(Boolean, default=False, nullable=False)  # True = requester-only note
    edited_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    task = relationship("TaskDB", backref="comments")
    author = relationship("UserDB", backref="task_comments", foreign_keys=[user_id])
    replies = relationship("TaskCommentDB", backref=backref("parent", remote_side=[id]))


class StripeEventLogDB(Base):
    """Log of processed Stripe webhook events (for idempotency)."""
    __tablename__ = "stripe_event_log"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stripe_event_id = Column(String(128), unique=True, nullable=False, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    payload = Column(JSON, nullable=True)
    processed = Column(Boolean, default=False, nullable=False)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("UserDB", backref="stripe_events")


class SavedSearchDB(Base):
    """Worker's saved search filters with optional task-alert notifications."""
    __tablename__ = "saved_searches"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    name = Column(String(120), nullable=False)
    # e.g. {"task_type": "label_image", "priority": "high", "q": "dog"}
    filters = Column(JSON, default=dict, nullable=False)
    alert_enabled = Column(Boolean, default=True, nullable=False)
    # instant | daily | weekly
    alert_frequency = Column(String(16), default="instant", nullable=False)
    last_notified_at = Column(DateTime(timezone=True), nullable=True)
    match_count = Column(Integer, default=0, nullable=False)  # lifetime matches
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    user = relationship("UserDB", back_populates="saved_searches")


class ApiKeyUsageLogDB(Base):
    """Per-request usage log for API key calls."""
    __tablename__ = "api_key_usage_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    api_key_id = Column(UUID(as_uuid=True), ForeignKey("api_keys.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    endpoint = Column(String(256), nullable=False)
    method = Column(String(8), nullable=False, default="GET")
    status_code = Column(Integer, nullable=False, default=200)
    response_time_ms = Column(Integer, nullable=True)
    credits_used = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    api_key = relationship("ApiKeyDB", backref="usage_logs")
    user = relationship("UserDB", backref="api_usage_logs")


class SkillQuizQuestionDB(Base):
    """Static question bank for worker skill assessments."""
    __tablename__ = "skill_quiz_questions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    skill_category = Column(String(64), nullable=False, index=True)
    question = Column(Text, nullable=False)
    options = Column(JSON, nullable=False)          # list[str] — 4 choices
    correct_index = Column(Integer, nullable=False)  # 0-3
    difficulty = Column(Integer, nullable=False, default=1)  # 1=easy 2=medium 3=hard
    explanation = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class SkillQuizAttemptDB(Base):
    """Record of a worker's quiz attempt for a skill category."""
    __tablename__ = "skill_quiz_attempts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    worker_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                       nullable=False, index=True)
    skill_category = Column(String(64), nullable=False)
    question_ids = Column(JSON, nullable=False)   # list[str] UUIDs of questions used
    answers = Column(JSON, nullable=False)         # list[int] worker's chosen indices
    score = Column(Integer, nullable=False)        # number correct
    total = Column(Integer, nullable=False)
    passed = Column(Boolean, nullable=False, default=False)
    proficiency_level = Column(Integer, nullable=False, default=1)  # 1-5, set on pass
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class DisputeEvidenceDB(Base):
    """Evidence submitted by a worker, requester, or mediator in a dispute."""
    __tablename__ = "dispute_evidence"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    submitter_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                          nullable=False, index=True)
    # Role of the submitter in this dispute
    submitter_role = Column(String(32), nullable=False)   # worker | requester | mediator
    evidence_type = Column(String(32), nullable=False, default="text")  # text | url | image_url
    content = Column(Text, nullable=False)                # Text description or URL
    assignment_id = Column(UUID(as_uuid=True),
                           ForeignKey("task_assignments.id", ondelete="SET NULL"),
                           nullable=True)                 # Which submission this supports
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    task = relationship("TaskDB", backref="evidence")
    submitter = relationship("UserDB", backref="dispute_evidence")


class DisputeEventDB(Base):
    """Audit trail / timeline event for a dispute."""
    __tablename__ = "dispute_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    actor_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"),
                      nullable=True)
    # Event types: dispute_opened | evidence_added | mediator_assigned |
    #              resolution_proposed | resolved | escalated
    event_type = Column(String(64), nullable=False)
    description = Column(Text, nullable=False)
    event_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)

    task = relationship("TaskDB", backref="dispute_events")
    actor = relationship("UserDB", backref="dispute_actions")

    worker = relationship("UserDB", backref="quiz_attempts")


class WebhookEndpointDB(Base):
    """
    Persistent webhook subscription — fires for all tasks owned by the user.

    Unlike per-task webhook_url, these endpoints are registered once and
    automatically receive events for every task this user creates.
    """
    __tablename__ = "webhook_endpoints"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    url = Column(String(2048), nullable=False)
    description = Column(String(255), nullable=True)
    # Which events to receive — NULL means ALL events
    events = Column(JSON, nullable=True)
    # HMAC-SHA256 signing secret — base64url random bytes
    secret = Column(String(128), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    # Stats (cached)
    delivery_count = Column(Integer, default=0, nullable=False)
    failure_count = Column(Integer, default=0, nullable=False)
    last_triggered_at = Column(DateTime(timezone=True), nullable=True)
    last_failure_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    user = relationship("UserDB", backref="webhook_endpoints")


# ── Task tags (stored inline on TaskDB.tags as JSON array) ───────────────────
# No separate table needed — tags are simple strings on the task.


class RequesterOnboardingDB(Base):
    """
    Tracks requester onboarding progress.

    Five guided steps encourage new requesters to explore core platform features:
      1. welcome          – complete profile / set display name
      2. create_task      – create first AI or human task
      3. view_results     – open a completed task's result page
      4. set_webhook      – register a webhook endpoint
      5. invite_team      – invite a team member (or skip)

    +200 credit bonus awarded on full completion.
    """
    __tablename__ = "requester_onboarding"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, unique=True, index=True)

    step_welcome = Column(Boolean, default=False, nullable=False)
    step_create_task = Column(Boolean, default=False, nullable=False)
    step_view_results = Column(Boolean, default=False, nullable=False)
    step_set_webhook = Column(Boolean, default=False, nullable=False)
    step_invite_team = Column(Boolean, default=False, nullable=False)

    completed_at = Column(DateTime(timezone=True), nullable=True)
    bonus_claimed = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("UserDB", backref="requester_onboarding")


# ─── Webhook Payload Templates ────────────────────────────────────────────────

class WebhookPayloadTemplateDB(Base):
    """
    Custom payload templates for webhook events.

    Users can define per-event-type JSON payload templates using
    {{field}} placeholders which are replaced with task context at delivery time.
    One template per (user_id, event_type) pair — upserted on creation.
    """
    __tablename__ = "webhook_payload_templates"
    __table_args__ = (UniqueConstraint("user_id", "event_type"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    event_type = Column(String(64), nullable=False)
    template = Column(Text, nullable=False)
    description = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    user = relationship("UserDB", backref="webhook_payload_templates")
