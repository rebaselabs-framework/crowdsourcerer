"""Canonical credit cost rules for every task type.

:class:`TaskPricing` is a frozen value object — construct a custom
instance to override pricing per deployment, or use
:data:`default_pricing` for the common path.
"""

from dataclasses import dataclass, field
from typing import Mapping, Protocol

from core.task_types import (
    AI_TASK_CREDITS,
    HUMAN_TASK_BASE_CREDITS,
    HUMAN_TASK_TYPES,
)

_AI_FALLBACK_CREDITS = 5


class TaskPricingError(ValueError):
    """Raised when a pricing input is inconsistent (negative reward, etc.)."""


class _CreateLike(Protocol):
    type: str
    worker_reward_credits: int | None
    assignments_required: int


class _TaskLike(Protocol):
    type: str
    execution_mode: str
    worker_reward_credits: int | None
    assignments_required: int | None


@dataclass(frozen=True, slots=True)
class TaskPricing:
    """Value object holding the rules for computing task credit costs."""

    # MappingProxyType / frozenset are immutable, but Python 3.11's
    # dataclass decorator still refuses them as inline defaults. Using
    # default_factory lambdas that return the same singleton is the
    # supported workaround — no per-instance allocation.
    ai_credits: Mapping[str, int] = field(default_factory=lambda: AI_TASK_CREDITS)
    human_base_credits: Mapping[str, int] = field(
        default_factory=lambda: HUMAN_TASK_BASE_CREDITS,
    )
    human_task_types: frozenset[str] = field(default_factory=lambda: HUMAN_TASK_TYPES)
    human_platform_fee_fraction: float = 0.20
    ai_fallback_credits: int = _AI_FALLBACK_CREDITS
    min_platform_fee: int = 1

    def is_human(self, task_type: str) -> bool:
        """True when *task_type* is routed through the human marketplace."""
        return task_type in self.human_task_types

    def compute_create_cost(self, req: _CreateLike) -> int:
        """Total credits to reserve when a requester submits *req*."""
        if self.is_human(req.type):
            return self._human_cost(
                req.type, req.worker_reward_credits, req.assignments_required,
            )
        return self._ai_cost(req.type)

    def compute_task_cost(self, task: _TaskLike) -> int:
        """Credits originally reserved for an existing task (refund path)."""
        if task.execution_mode == "human":
            return self._human_cost(
                task.type, task.worker_reward_credits, task.assignments_required or 1,
            )
        return self._ai_cost(task.type)

    # ─── Internals ────────────────────────────────────────────────────────

    def _ai_cost(self, task_type: str) -> int:
        return self.ai_credits.get(task_type, self.ai_fallback_credits)

    def _human_cost(
        self,
        task_type: str,
        worker_reward: int | None,
        assignments_required: int,
    ) -> int:
        reward = worker_reward or self.human_base_credits.get(task_type, 2)
        if reward < 0:
            raise TaskPricingError(f"worker_reward_credits must be ≥ 0, got {reward}")
        if assignments_required < 1:
            raise TaskPricingError(
                f"assignments_required must be ≥ 1, got {assignments_required}"
            )
        subtotal = reward * assignments_required
        fee = max(self.min_platform_fee, int(subtotal * self.human_platform_fee_fraction))
        return subtotal + fee


default_pricing: TaskPricing = TaskPricing()


__all__ = [
    "TaskPricing",
    "TaskPricingError",
    "default_pricing",
    "AI_TASK_CREDITS",
    "HUMAN_TASK_BASE_CREDITS",
    "HUMAN_TASK_TYPES",
]
