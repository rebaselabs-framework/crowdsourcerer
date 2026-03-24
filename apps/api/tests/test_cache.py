"""Unit tests for cache utility functions (_input_hash, _ttl_hours).

These are pure-function tests — no database or external services needed.
"""
from __future__ import annotations

import os

# Set required env vars before any app imports
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")

import pytest
from core.result_cache import _input_hash, _ttl_hours, _DEFAULT_TTL_HOURS


# ── _input_hash ────────────────────────────────────────────────────────────

def test_input_hash_is_hex_string():
    """Hash should be a 64-character lowercase hex string (SHA-256)."""
    h = _input_hash("llm_generate", {"prompt": "hello"})
    assert isinstance(h, str)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_input_hash_ordering_independence():
    """Dict key ordering must not affect the hash."""
    h1 = _input_hash("data_transform", {"b": 1, "a": 2})
    h2 = _input_hash("data_transform", {"a": 2, "b": 1})
    assert h1 == h2


def test_input_hash_type_affects_hash():
    """Same input with different task types should produce different hashes."""
    h1 = _input_hash("llm_generate", {"prompt": "hello"})
    h2 = _input_hash("web_research", {"prompt": "hello"})
    assert h1 != h2


def test_input_hash_content_affects_hash():
    """Different inputs must produce different hashes."""
    h1 = _input_hash("llm_generate", {"prompt": "hello"})
    h2 = _input_hash("llm_generate", {"prompt": "world"})
    assert h1 != h2


def test_input_hash_deterministic():
    """Same call always returns the same hash."""
    inp = {"url": "https://example.com", "depth": 2}
    assert _input_hash("web_research", inp) == _input_hash("web_research", inp)


def test_input_hash_nested_ordering_independence():
    """Nested dicts should also be order-independent."""
    h1 = _input_hash("data_transform", {"opts": {"z": 3, "y": 4}})
    h2 = _input_hash("data_transform", {"opts": {"y": 4, "z": 3}})
    assert h1 == h2


# ── _ttl_hours ─────────────────────────────────────────────────────────────

def test_ttl_hours_deterministic_types_never_expire():
    """Purely deterministic task types should have TTL of 0 (never expire)."""
    for task_type in ("audio_transcribe", "document_parse", "data_transform", "pii_detect", "code_execute"):
        assert _ttl_hours(task_type) == 0, f"Expected TTL 0 for {task_type}"


def test_ttl_hours_web_research_short():
    """web_research is time-sensitive and should have a short TTL."""
    ttl = _ttl_hours("web_research")
    assert 0 < ttl <= 6


def test_ttl_hours_unknown_type_uses_default():
    """An unknown task type should fall back to a sensible non-zero TTL."""
    ttl = _ttl_hours("completely_unknown_type_xyz")
    assert ttl > 0


def test_ttl_hours_returns_int():
    """TTL should always be an integer."""
    for task_type in list(_DEFAULT_TTL_HOURS.keys()) + ["unknown_type"]:
        assert isinstance(_ttl_hours(task_type), int)
