"""Unit tests for services/pricing.py — pure domain logic, no DB."""

from dataclasses import dataclass
from typing import Any

import pytest

from services.pricing import TaskPricing, TaskPricingError, default_pricing


# ── Lightweight stand-ins for TaskCreateRequest / TaskDB ─────────────────


@dataclass
class _FakeCreate:
    type: str
    worker_reward_credits: int | None = None
    assignments_required: int = 1


@dataclass
class _FakeTask:
    type: str
    execution_mode: str
    worker_reward_credits: int | None = None
    assignments_required: int | None = 1


# ── AI pricing ────────────────────────────────────────────────────────────


class TestAIPricing:
    def test_ai_create_uses_table_lookup(self):
        req = _FakeCreate(type="web_research")
        assert default_pricing.compute_create_cost(req) == 10

    def test_ai_task_uses_table_lookup(self):
        task = _FakeTask(type="llm_generate", execution_mode="ai")
        assert default_pricing.compute_task_cost(task) == 1

    def test_unknown_ai_type_falls_back(self):
        req = _FakeCreate(type="definitely_not_a_real_type")
        assert default_pricing.compute_create_cost(req) == default_pricing.ai_fallback_credits

    def test_is_human_rejects_ai_types(self):
        assert default_pricing.is_human("web_research") is False
        assert default_pricing.is_human("llm_generate") is False


# ── Human pricing ─────────────────────────────────────────────────────────


class TestHumanPricing:
    def test_human_create_default_reward_and_fee(self):
        req = _FakeCreate(type="label_image", assignments_required=1)
        # label_image base = 3, fee = max(1, int(3 * 1 * 0.2)) = max(1, 0) = 1
        assert default_pricing.compute_create_cost(req) == 4

    def test_human_create_custom_reward(self):
        req = _FakeCreate(
            type="label_image",
            worker_reward_credits=10,
            assignments_required=3,
        )
        # subtotal = 30, fee = max(1, int(30 * 0.2)) = 6
        assert default_pricing.compute_create_cost(req) == 36

    def test_human_task_matches_create_cost(self):
        """compute_task_cost on a persisted row equals compute_create_cost
        on the request it came from — this is the invariant that the
        previous two copy-pasted functions were trying (and failing) to
        maintain."""
        req = _FakeCreate(
            type="rate_quality",
            worker_reward_credits=8,
            assignments_required=5,
        )
        task = _FakeTask(
            type="rate_quality",
            execution_mode="human",
            worker_reward_credits=8,
            assignments_required=5,
        )
        assert default_pricing.compute_task_cost(task) == default_pricing.compute_create_cost(req)

    def test_min_platform_fee_floor(self):
        """Tiny rewards still collect the minimum platform fee."""
        req = _FakeCreate(
            type="rate_quality",
            worker_reward_credits=1,
            assignments_required=1,
        )
        # subtotal=1, raw fee=0 → floor to 1 → total 2
        assert default_pricing.compute_create_cost(req) == 2

    def test_is_human_accepts_all_human_types(self):
        for t in default_pricing.human_base_credits:
            assert default_pricing.is_human(t) is True

    def test_negative_reward_raises(self):
        req = _FakeCreate(
            type="label_image",
            worker_reward_credits=-5,
            assignments_required=1,
        )
        with pytest.raises(TaskPricingError):
            default_pricing.compute_create_cost(req)

    def test_zero_assignments_raises(self):
        req = _FakeCreate(
            type="label_image",
            worker_reward_credits=5,
            assignments_required=0,
        )
        with pytest.raises(TaskPricingError):
            default_pricing.compute_create_cost(req)


# ── Custom pricing instance (configurability) ────────────────────────────


class TestCustomPricing:
    def test_custom_fee_fraction(self):
        cheap = TaskPricing(human_platform_fee_fraction=0.05)
        req = _FakeCreate(
            type="label_image",
            worker_reward_credits=100,
            assignments_required=1,
        )
        # subtotal=100, fee=max(1, int(100 * 0.05))=5 → 105
        assert cheap.compute_create_cost(req) == 105

    def test_custom_ai_table_overrides_defaults(self):
        free_ai = TaskPricing(ai_credits={"web_research": 0})
        req = _FakeCreate(type="web_research")
        assert free_ai.compute_create_cost(req) == 0

    def test_custom_fallback_credits(self):
        cheap = TaskPricing(ai_fallback_credits=1)
        req = _FakeCreate(type="unknown_type")
        assert cheap.compute_create_cost(req) == 1

    def test_pricing_is_frozen(self):
        """TaskPricing is a value object — mutation attempts fail."""
        with pytest.raises((AttributeError, Exception)):
            default_pricing.human_platform_fee_fraction = 0.5  # type: ignore[misc]
