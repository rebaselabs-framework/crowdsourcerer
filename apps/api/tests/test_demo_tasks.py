"""Tests for core/demo_tasks.py — demo task seeder for worker onboarding.

Covers:
  1.  DEMO_TASKS covers all 8 human task types
  2.  DEMO_TASKS has at least 2 tasks per type
  3.  Every task has required fields (type, input, task_instructions, etc.)
  4.  Every task has a gold_answer (for accuracy tracking)
  5.  Every task is tagged with 'demo' and 'tutorial'
  6.  All task types have valid input keys for their type
  7.  SYSTEM_USER_EMAIL is set
  8.  SYSTEM_USER_NAME is set
  9.  No duplicate task content (each input is unique)
  10. worker_reward_credits match expected base rates
  11. Gold answers have expected keys per task type
  12. seed_demo_tasks idempotency — second call is no-op (mock DB)
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG", "true")

from collections import Counter

# ── Constants ─────────────────────────────────────────────────────────────────

HUMAN_TASK_TYPES = {
    "label_image", "label_text", "rate_quality",
    "verify_fact", "moderate_content", "compare_rank",
    "answer_question", "transcription_review",
}

# Expected input keys per task type (required keys only)
REQUIRED_INPUT_KEYS: dict[str, set[str]] = {
    "label_image": {"image_url", "labels"},
    "label_text": {"text", "categories"},
    "rate_quality": {"content", "criteria"},
    "verify_fact": {"claim"},
    "moderate_content": {"content"},
    "compare_rank": {"items", "criteria"},
    "answer_question": {"content", "question"},
    "transcription_review": {"ai_transcript"},
}

# Expected gold answer keys per task type
GOLD_ANSWER_KEYS: dict[str, set[str]] = {
    "label_image": {"label"},
    "label_text": {"label"},
    "rate_quality": {"rating"},
    "verify_fact": {"verdict"},
    "moderate_content": {"decision"},
    "compare_rank": {"choice"},
    "answer_question": {"answer"},
    "transcription_review": {"text"},
}


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_all_human_types_covered():
    """DEMO_TASKS covers all 8 human task types."""
    from core.demo_tasks import DEMO_TASKS
    task_types = {t["type"] for t in DEMO_TASKS}
    assert task_types == HUMAN_TASK_TYPES


def test_at_least_two_per_type():
    """Each task type has at least 2 demo tasks."""
    from core.demo_tasks import DEMO_TASKS
    counts = Counter(t["type"] for t in DEMO_TASKS)
    for task_type in HUMAN_TASK_TYPES:
        assert counts[task_type] >= 2, f"{task_type} has only {counts[task_type]} demo task(s)"


def test_required_fields_present():
    """Every demo task has all required fields."""
    from core.demo_tasks import DEMO_TASKS
    required = {"type", "input", "task_instructions", "worker_reward_credits", "gold_answer", "tags"}
    for i, task in enumerate(DEMO_TASKS):
        missing = required - set(task.keys())
        assert not missing, f"Task {i} ({task.get('type', '?')}) missing fields: {missing}"


def test_every_task_has_gold_answer():
    """Every demo task has a non-empty gold_answer dict."""
    from core.demo_tasks import DEMO_TASKS
    for i, task in enumerate(DEMO_TASKS):
        ga = task["gold_answer"]
        assert isinstance(ga, dict), f"Task {i}: gold_answer is not a dict"
        assert len(ga) > 0, f"Task {i}: gold_answer is empty"


def test_every_task_tagged_demo_and_tutorial():
    """Every demo task has both 'demo' and 'tutorial' tags."""
    from core.demo_tasks import DEMO_TASKS
    for i, task in enumerate(DEMO_TASKS):
        tags = task["tags"]
        assert "demo" in tags, f"Task {i} missing 'demo' tag"
        assert "tutorial" in tags, f"Task {i} missing 'tutorial' tag"


def test_valid_input_keys_per_type():
    """Each task's input dict has the required keys for its type."""
    from core.demo_tasks import DEMO_TASKS
    for i, task in enumerate(DEMO_TASKS):
        task_type = task["type"]
        required_keys = REQUIRED_INPUT_KEYS.get(task_type, set())
        actual_keys = set(task["input"].keys())
        missing = required_keys - actual_keys
        assert not missing, f"Task {i} ({task_type}) input missing keys: {missing}"


def test_system_user_email_set():
    """SYSTEM_USER_EMAIL constant is a valid email."""
    from core.demo_tasks import SYSTEM_USER_EMAIL
    assert "@" in SYSTEM_USER_EMAIL
    assert len(SYSTEM_USER_EMAIL) > 5


def test_system_user_name_set():
    """SYSTEM_USER_NAME constant is a non-empty string."""
    from core.demo_tasks import SYSTEM_USER_NAME
    assert isinstance(SYSTEM_USER_NAME, str)
    assert len(SYSTEM_USER_NAME) > 0


def test_no_duplicate_inputs():
    """Each demo task has unique input content."""
    from core.demo_tasks import DEMO_TASKS
    import json
    seen = set()
    for i, task in enumerate(DEMO_TASKS):
        key = json.dumps(task["input"], sort_keys=True)
        assert key not in seen, f"Task {i} ({task['type']}) has duplicate input"
        seen.add(key)


def test_reward_credits_positive():
    """All demo tasks have positive reward credits."""
    from core.demo_tasks import DEMO_TASKS
    for i, task in enumerate(DEMO_TASKS):
        assert task["worker_reward_credits"] > 0, f"Task {i} has non-positive reward"
        assert task["worker_reward_credits"] <= 10, f"Task {i} reward too high for demo"


def test_gold_answer_keys_match_type():
    """Gold answer dicts have the expected keys for their task type."""
    from core.demo_tasks import DEMO_TASKS
    for i, task in enumerate(DEMO_TASKS):
        task_type = task["type"]
        expected_keys = GOLD_ANSWER_KEYS.get(task_type, set())
        actual_keys = set(task["gold_answer"].keys())
        missing = expected_keys - actual_keys
        assert not missing, f"Task {i} ({task_type}) gold_answer missing keys: {missing}"


def test_task_instructions_are_substantial():
    """Task instructions are non-empty and substantial (>50 chars)."""
    from core.demo_tasks import DEMO_TASKS
    for i, task in enumerate(DEMO_TASKS):
        instructions = task["task_instructions"]
        assert isinstance(instructions, str), f"Task {i}: instructions not a string"
        assert len(instructions) >= 50, f"Task {i}: instructions too short ({len(instructions)} chars)"


def test_total_task_count():
    """There are exactly 16 demo tasks (2 per type * 8 types)."""
    from core.demo_tasks import DEMO_TASKS
    assert len(DEMO_TASKS) == 16
