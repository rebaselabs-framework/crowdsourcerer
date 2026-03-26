"""Unit tests for quality control answer comparison logic.

Tests cover the _compare_answers() function for all supported task types.
No DB or HTTP required — pure business logic testing.
"""
from __future__ import annotations
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")


def compare(task_type, worker, gold):
    from routers.quality import _compare_answers
    return _compare_answers(task_type, worker, gold)


# ── label_image / label_text ──────────────────────────────────────────────────

def test_label_image_exact_match():
    assert compare("label_image", {"label": "cat"}, {"label": "cat"}) is True


def test_label_image_case_insensitive():
    assert compare("label_image", {"label": "CAT"}, {"label": "cat"}) is True


def test_label_image_mismatch():
    assert compare("label_image", {"label": "dog"}, {"label": "cat"}) is False


def test_label_image_multi_label_order_independent():
    """Multi-label answers should match regardless of order."""
    worker = {"labels": ["cat", "animal"]}
    gold = {"labels": ["animal", "cat"]}
    assert compare("label_image", worker, gold) is True


def test_label_image_multi_label_mismatch():
    worker = {"labels": ["cat", "dog"]}
    gold = {"labels": ["cat", "bird"]}
    assert compare("label_image", worker, gold) is False


def test_label_text_same_as_label_image():
    assert compare("label_text", {"label": "positive"}, {"label": "positive"}) is True
    assert compare("label_text", {"label": "positive"}, {"label": "negative"}) is False


# ── rate_quality ──────────────────────────────────────────────────────────────

def test_rate_quality_exact():
    assert compare("rate_quality", {"rating": 4}, {"rating": 4}) is True


def test_rate_quality_within_tolerance():
    """±1 is acceptable."""
    assert compare("rate_quality", {"rating": 3}, {"rating": 4}) is True
    assert compare("rate_quality", {"rating": 5}, {"rating": 4}) is True


def test_rate_quality_outside_tolerance():
    """>1 difference is not acceptable."""
    assert compare("rate_quality", {"rating": 2}, {"rating": 4}) is False


def test_rate_quality_float_precision():
    """Float ratings should also work within tolerance."""
    assert compare("rate_quality", {"rating": 3.5}, {"rating": 4.0}) is True
    assert compare("rate_quality", {"rating": 2.4}, {"rating": 4.0}) is False


def test_rate_quality_bad_value():
    """Non-numeric ratings should not crash — return False."""
    result = compare("rate_quality", {"rating": "great"}, {"rating": 4})
    assert result is False


# ── verify_fact ───────────────────────────────────────────────────────────────

def test_verify_fact_true_match():
    assert compare("verify_fact", {"verdict": "true"}, {"verdict": "true"}) is True


def test_verify_fact_false_match():
    assert compare("verify_fact", {"verdict": "false"}, {"verdict": "false"}) is True


def test_verify_fact_mismatch():
    assert compare("verify_fact", {"verdict": "true"}, {"verdict": "false"}) is False


def test_verify_fact_case_insensitive():
    assert compare("verify_fact", {"verdict": "TRUE"}, {"verdict": "true"}) is True


def test_verify_fact_unsupported():
    assert compare("verify_fact", {"verdict": "unsupported"}, {"verdict": "unsupported"}) is True


# ── moderate_content ──────────────────────────────────────────────────────────

def test_moderate_content_match():
    assert compare("moderate_content", {"decision": "approve"}, {"decision": "approve"}) is True


def test_moderate_content_mismatch():
    assert compare("moderate_content", {"decision": "approve"}, {"decision": "reject"}) is False


def test_moderate_content_case_insensitive():
    assert compare("moderate_content", {"decision": "APPROVE"}, {"decision": "approve"}) is True


# ── compare_rank ──────────────────────────────────────────────────────────────

def test_compare_rank_exact():
    worker = {"ranked_ids": ["a", "b", "c"]}
    gold = {"ranked_ids": ["a", "b", "c"]}
    assert compare("compare_rank", worker, gold) is True


def test_compare_rank_wrong_order():
    worker = {"ranked_ids": ["b", "a", "c"]}
    gold = {"ranked_ids": ["a", "b", "c"]}
    assert compare("compare_rank", worker, gold) is False


def test_compare_rank_coerces_to_str():
    """Ranked IDs should be compared as strings."""
    worker = {"ranked_ids": [1, 2, 3]}
    gold = {"ranked_ids": ["1", "2", "3"]}
    assert compare("compare_rank", worker, gold) is True


# ── answer_question / transcription_review ───────────────────────────────────

def test_answer_question_exact():
    worker = {"answer": "Paris"}
    gold = {"answer": "Paris"}
    assert compare("answer_question", worker, gold) is True


def test_answer_question_case_insensitive():
    worker = {"answer": "paris"}
    gold = {"answer": "Paris"}
    assert compare("answer_question", worker, gold) is True


def test_answer_question_high_similarity():
    """≥80% character overlap should pass."""
    # Very similar strings
    worker = {"answer": "the capital of france is paris"}
    gold = {"answer": "the capital of france is paris!"}
    assert compare("answer_question", worker, gold) is True


def test_answer_question_low_similarity():
    """Completely different answers should fail."""
    worker = {"answer": "tokyo"}
    gold = {"answer": "paris"}
    assert compare("answer_question", worker, gold) is False


def test_answer_question_empty_both():
    """Both empty → match."""
    assert compare("answer_question", {"answer": ""}, {"answer": ""}) is True


def test_answer_question_one_empty():
    """Worker submits empty, gold is not — should not match."""
    assert compare("answer_question", {"answer": ""}, {"answer": "paris"}) is False


# ── Default: non-dict fallback ────────────────────────────────────────────────

def test_default_string_fallback_match():
    assert compare("unknown_type", "yes", "yes") is True


def test_default_string_fallback_case_insensitive():
    assert compare("unknown_type", "YES", "yes") is True


def test_default_string_fallback_mismatch():
    assert compare("unknown_type", "yes", "no") is False


def test_default_dict_exact_match():
    """For unknown type with dict inputs, exact equality is required."""
    assert compare("custom_type", {"key": "val"}, {"key": "val"}) is True
    assert compare("custom_type", {"key": "val"}, {"key": "other"}) is False
