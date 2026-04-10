"""Task pricing service.

Owns the canonical cost rules for every task type. The previous
implementation duplicated ``_calc_credits`` (in ``routers/tasks.py``)
and ``_compute_task_cost`` — the former took a create request, the
latter an already-persisted row, but both computed the same thing.

The ``TaskPricing`` dataclass is the generic core:

- ``ai_credits``                  — flat cost per AI task type
- ``human_base_credits``          — default worker reward per human type
- ``human_platform_fee_fraction`` — fraction of (worker_reward *
                                    assignments_required) added as
                                    platform fee, floored at 1 credit.

Callers that need a different pricing policy (e.g. a staging env with
free AI tasks or a white-label with a different fee split) construct a
custom instance and pass it in. Most code uses :data:`default_pricing`.
"""

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from workers.router import TASK_CREDITS as _AI_TASK_CREDITS


# ── Default tables ────────────────────────────────────────────────────────

# Default per-type worker reward for human tasks. Keep in sync with the
# frontend task-type picker in ``apps/web/src/pages/dashboard/new-task.astro``.
_HUMAN_TASK_BASE_CREDITS: Mapping[str, int] = {
    "label_image": 3,
    "label_text": 2,
    "rate_quality": 2,
    "verify_fact": 3,
    "moderate_content": 2,
    "compare_rank": 2,
    "answer_question": 4,
    "transcription_review": 5,
}

_HUMAN_TASK_TYPES: frozenset[str] = frozenset(_HUMAN_TASK_BASE_CREDITS)

_AI_FALLBACK_CREDITS = 5


class TaskPricingError(ValueError):
    """Raised when a pricing input is inconsistent (negative reward, etc.)."""


class _CreateLike(Protocol):
    """Structural subset of ``TaskCreateRequest`` used by pricing.

    Using a Protocol avoids a hard dependency on ``models.schemas`` so
    the pricing service can be unit tested with simple namespaces.
    """

    type: str
    worker_reward_credits: int | None
    assignments_required: int


class _TaskLike(Protocol):
    """Structural subset of ``TaskDB`` used by pricing."""

    type: str
    execution_mode: str
    worker_reward_credits: int | None
    assignments_required: int | None


@dataclass(frozen=True, slots=True)
class TaskPricing:
    """Value object holding the rules for computing task credit costs."""

    ai_credits: Mapping[str, int] = field(default_factory=lambda: dict(_AI_TASK_CREDITS))
    human_base_credits: Mapping[str, int] = field(
        default_factory=lambda: dict(_HUMAN_TASK_BASE_CREDITS)
    )
    human_task_types: frozenset[str] = field(default_factory=lambda: _HUMAN_TASK_TYPES)
    human_platform_fee_fraction: float = 0.20
    ai_fallback_credits: int = _AI_FALLBACK_CREDITS
    min_platform_fee: int = 1

    # ─── Public API ───────────────────────────────────────────────────────

    def is_human(self, task_type: str) -> bool:
        """True when *task_type* is routed through the human marketplace."""
        return task_type in self.human_task_types

    def compute_create_cost(self, req: _CreateLike) -> int:
        """Total credits to reserve when a requester submits *req*.

        For human tasks the cost is ``worker_reward * assignments + fee``
        where ``fee = max(min_platform_fee, floor(reward * assignments * fraction))``.
        For AI tasks it's the flat per-type table lookup with a safe default.
        """
        if self.is_human(req.type):
            return self._human_cost(
                task_type=req.type,
                worker_reward=req.worker_reward_credits,
                assignments_required=req.assignments_required,
            )
        return self._ai_cost(req.type)

    def compute_task_cost(self, task: _TaskLike) -> int:
        """Recompute the credits originally reserved for an existing task.

        Used on refund paths (``_refund_task_credits`` in tasks.py) and
        everywhere else we need the "what did this cost" answer without
        re-querying the requester.
        """
        if task.execution_mode == "human":
            return self._human_cost(
                task_type=task.type,
                worker_reward=task.worker_reward_credits,
                assignments_required=task.assignments_required or 1,
            )
        return self._ai_cost(task.type)

    # ─── Internals ────────────────────────────────────────────────────────

    def _ai_cost(self, task_type: str) -> int:
        return self.ai_credits.get(task_type, self.ai_fallback_credits)

    def _human_cost(
        self,
        *,
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


# Module-level singleton — use this from routers unless you need custom rules.
default_pricing: TaskPricing = TaskPricing()


__all__ = [
    "TaskPricing",
    "TaskPricingError",
    "default_pricing",
    "_HUMAN_TASK_BASE_CREDITS",
    "_HUMAN_TASK_TYPES",
]
