"""SQLAlchemy ORM models."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, Integer, BigInteger, Boolean, DateTime,
    Text, ForeignKey, Enum as SAEnum, JSON
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
    credits = Column(Integer, default=100, nullable=False)
    stripe_customer_id = Column(String(255), nullable=True, unique=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    tasks = relationship("TaskDB", back_populates="user", lazy="dynamic")
    api_keys = relationship("ApiKeyDB", back_populates="user", lazy="dynamic")
    transactions = relationship("CreditTransactionDB", back_populates="user", lazy="dynamic")


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
            "web_research", "entity_lookup", "document_parse", "data_transform",
            "llm_generate", "screenshot", "audio_transcribe", "pii_detect",
            "code_execute", "web_intel",
            name="task_type_enum",
        ),
        nullable=False,
    )
    status = Column(
        SAEnum("pending", "queued", "running", "completed", "failed", "cancelled",
               name="task_status_enum"),
        default="pending",
        nullable=False,
    )
    priority = Column(
        SAEnum("low", "normal", "high", "urgent", name="task_priority_enum"),
        default="normal",
        nullable=False,
    )
    input = Column(JSON, nullable=False)
    output = Column(JSON, nullable=True)
    error = Column(Text, nullable=True)
    credits_used = Column(Integer, nullable=True)
    duration_ms = Column(BigInteger, nullable=True)
    webhook_url = Column(String(2048), nullable=True)
    metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("UserDB", back_populates="tasks")


class CreditTransactionDB(Base):
    __tablename__ = "credit_transactions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True)
    amount = Column(Integer, nullable=False)  # positive = credit, negative = charge
    type = Column(
        SAEnum("charge", "credit", "refund", name="transaction_type_enum"),
        nullable=False,
    )
    description = Column(String(512), nullable=False)
    stripe_payment_intent = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    user = relationship("UserDB", back_populates="transactions")
