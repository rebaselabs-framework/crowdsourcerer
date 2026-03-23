"""SQLAlchemy ORM models."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Integer, BigInteger, Boolean, DateTime, Float,
    Text, ForeignKey, Enum as SAEnum, JSON, Date, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

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

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    tasks = relationship("TaskDB", back_populates="user", lazy="dynamic")
    api_keys = relationship("ApiKeyDB", back_populates="user", lazy="dynamic")
    transactions = relationship("CreditTransactionDB", back_populates="user", lazy="dynamic")
    assignments = relationship("TaskAssignmentDB", back_populates="worker", lazy="dynamic",
                               foreign_keys="TaskAssignmentDB.worker_id")
    badges = relationship("WorkerBadgeDB", back_populates="user", lazy="dynamic")


class ApiKeyDB(Base):
    __tablename__ = "api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(255), nullable=False)
    key_hash = Column(String(64), unique=True, nullable=False, index=True)
    key_prefix = Column(String(16), nullable=False)
    scopes = Column(JSON, default=list, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("UserDB", back_populates="api_keys")


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

    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("UserDB", back_populates="tasks")
    assignments = relationship("TaskAssignmentDB", back_populates="task", lazy="dynamic")


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

    task = relationship("TaskDB", back_populates="assignments")
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
    attempt = Column(Integer, default=1, nullable=False)  # 1-indexed
    status_code = Column(Integer, nullable=True)          # HTTP status if response received
    success = Column(Boolean, default=False, nullable=False)
    error = Column(Text, nullable=True)                   # Error message if failed
    duration_ms = Column(Integer, nullable=True)
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
    last_task_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    worker = relationship("UserDB", backref="skills", foreign_keys=[worker_id])


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
