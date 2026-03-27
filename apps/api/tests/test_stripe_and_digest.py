"""Tests for Stripe webhook pure helpers and notification digest pure helpers.

Stripe webhook (routers/stripe_webhooks.py) — _verify_stripe_signature:
  1.  Valid signature → True
  2.  Wrong secret → False
  3.  Tampered payload → False
  4.  Stale timestamp (> 5 min ago) → False
  5.  Future timestamp (> 5 min in future) → False
  6.  Missing 't' field in header → False (exception swallowed)
  7.  Missing 'v1' field → False (empty sig → digest mismatch)
  8.  Malformed header string → False (exception swallowed)
  9.  Empty payload → valid when signature matches
  10. Unicode payload round-trip

Notification digest (routers/notification_digest.py) — _digest_prefs_dict:
  11. Stored=None-like (empty dict) → returns all defaults
  12. Stored prefs override individual defaults
  13. All defaults present in output
  14. Extra stored keys pass through
  15. Default enabled=True
  16. Default frequency='daily'
  17. Default send_at_hour=8

  18. DigestPrefsUpdate Pydantic validation: valid frequency 'daily'
  19. DigestPrefsUpdate: valid frequency 'weekly'
  20. DigestPrefsUpdate: invalid frequency → ValidationError
  21. DigestPrefsUpdate: send_at_hour=0 (boundary) → valid
  22. DigestPrefsUpdate: send_at_hour=23 (boundary) → valid
  23. DigestPrefsUpdate: send_at_hour=24 → ValidationError
  24. DigestPrefsUpdate: send_at_hour=-1 → ValidationError
  25. DigestPrefsUpdate: all None fields → model_dump(exclude_none=True) returns {}

_DIGEST_DEFAULTS data integrity:
  26. All expected default keys present
  27. enabled is boolean True
  28. send_at_hour is int in [0, 23]
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest


# ── Test helpers ──────────────────────────────────────────────────────────────

def _make_stripe_sig_header(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Generate a valid Stripe-Signature header for a given payload and secret."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed = f"{ts}.{payload.decode('utf-8')}"
    sig = hmac.new(
        secret.encode("utf-8"),
        signed.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"t={ts},v1={sig}"


# ── _verify_stripe_signature ──────────────────────────────────────────────────

def test_valid_signature_returns_true():
    """A correctly signed payload returns True."""
    from routers.stripe_webhooks import _verify_stripe_signature
    secret  = "whsec_test_secret"
    payload = b'{"type": "checkout.session.completed"}'
    header  = _make_stripe_sig_header(payload, secret)
    assert _verify_stripe_signature(payload, header, secret) is True


def test_wrong_secret_returns_false():
    """Different secret → HMAC mismatch → False."""
    from routers.stripe_webhooks import _verify_stripe_signature
    payload = b'{"event": "test"}'
    header  = _make_stripe_sig_header(payload, "correct_secret")
    assert _verify_stripe_signature(payload, header, "wrong_secret") is False


def test_tampered_payload_returns_false():
    """Modified payload after signing → False."""
    from routers.stripe_webhooks import _verify_stripe_signature
    secret  = "whsec_test"
    original = b'{"amount": 100}'
    header   = _make_stripe_sig_header(original, secret)
    tampered = b'{"amount": 9999}'
    assert _verify_stripe_signature(tampered, header, secret) is False


def test_stale_timestamp_returns_false():
    """Timestamp > 300 seconds in the past → False (replay protection)."""
    from routers.stripe_webhooks import _verify_stripe_signature
    secret  = "whsec_test"
    payload = b'{"event": "test"}'
    old_ts  = int(time.time()) - 400     # 400 seconds ago = stale
    header  = _make_stripe_sig_header(payload, secret, old_ts)
    assert _verify_stripe_signature(payload, header, secret) is False


def test_future_timestamp_returns_false():
    """Timestamp > 300 seconds in the future → False (clock skew protection)."""
    from routers.stripe_webhooks import _verify_stripe_signature
    secret  = "whsec_test"
    payload = b'{"event": "test"}'
    future_ts = int(time.time()) + 400   # 400 seconds in future
    header    = _make_stripe_sig_header(payload, secret, future_ts)
    assert _verify_stripe_signature(payload, header, secret) is False


def test_missing_t_field_returns_false():
    """Header without 't=' field raises KeyError, swallowed → False."""
    from routers.stripe_webhooks import _verify_stripe_signature
    assert _verify_stripe_signature(b"payload", "v1=abc123", "secret") is False


def test_missing_v1_field_returns_false():
    """Header with only 't=' and no 'v1=' → empty sig → mismatch → False."""
    from routers.stripe_webhooks import _verify_stripe_signature
    ts     = int(time.time())
    header = f"t={ts}"    # no v1= field; sig.get("v1") returns ""
    assert _verify_stripe_signature(b"payload", header, "secret") is False


def test_malformed_header_returns_false():
    """Completely malformed header → exception swallowed → False."""
    from routers.stripe_webhooks import _verify_stripe_signature
    assert _verify_stripe_signature(b"payload", "not_a_valid_header!!!!", "secret") is False


def test_empty_payload_valid_signature():
    """Empty payload is valid as long as the signature matches."""
    from routers.stripe_webhooks import _verify_stripe_signature
    secret  = "whsec_empty"
    payload = b""
    header  = _make_stripe_sig_header(payload, secret)
    assert _verify_stripe_signature(payload, header, secret) is True


def test_signature_tolerance_boundary():
    """Timestamp exactly 299 seconds ago → still valid (within 300s window)."""
    from routers.stripe_webhooks import _verify_stripe_signature
    secret  = "whsec_tol"
    payload = b'{"event": "boundary"}'
    near_ts = int(time.time()) - 299   # just within tolerance
    header  = _make_stripe_sig_header(payload, secret, near_ts)
    assert _verify_stripe_signature(payload, header, secret) is True


# ── _digest_prefs_dict ────────────────────────────────────────────────────────

def _make_prefs_mock(stored: dict | None = None):
    """Create a minimal mock of NotificationPreferencesDB for _digest_prefs_dict."""
    from unittest.mock import MagicMock
    prefs = MagicMock()
    prefs.notification_digest_prefs = stored
    return prefs


def test_digest_prefs_dict_empty_returns_all_defaults():
    """notification_digest_prefs=None → all default keys returned."""
    from routers.notification_digest import _digest_prefs_dict, _DIGEST_DEFAULTS
    prefs  = _make_prefs_mock(None)
    result = _digest_prefs_dict(prefs)
    for key, val in _DIGEST_DEFAULTS.items():
        assert key in result, f"Default key '{key}' missing"
        assert result[key] == val, f"Default value mismatch for '{key}'"


def test_digest_prefs_dict_stored_overrides_defaults():
    """Stored prefs key overrides the corresponding default."""
    from routers.notification_digest import _digest_prefs_dict
    prefs  = _make_prefs_mock({"frequency": "weekly", "send_at_hour": 18})
    result = _digest_prefs_dict(prefs)
    assert result["frequency"] == "weekly"    # stored overrides default "daily"
    assert result["send_at_hour"] == 18       # stored overrides default 8


def test_digest_prefs_dict_all_defaults_present():
    """All six default keys are in the output."""
    from routers.notification_digest import _digest_prefs_dict, _DIGEST_DEFAULTS
    prefs  = _make_prefs_mock({})
    result = _digest_prefs_dict(prefs)
    assert set(result.keys()) >= set(_DIGEST_DEFAULTS.keys())


def test_digest_prefs_dict_extra_keys_pass_through():
    """Keys in stored prefs that aren't in defaults are preserved."""
    from routers.notification_digest import _digest_prefs_dict
    prefs  = _make_prefs_mock({"custom_key": "custom_value"})
    result = _digest_prefs_dict(prefs)
    assert result.get("custom_key") == "custom_value"


def test_digest_prefs_dict_default_enabled_true():
    from routers.notification_digest import _digest_prefs_dict
    result = _digest_prefs_dict(_make_prefs_mock(None))
    assert result["enabled"] is True


def test_digest_prefs_dict_default_frequency_daily():
    from routers.notification_digest import _digest_prefs_dict
    result = _digest_prefs_dict(_make_prefs_mock(None))
    assert result["frequency"] == "daily"


def test_digest_prefs_dict_default_send_at_hour_8():
    from routers.notification_digest import _digest_prefs_dict
    result = _digest_prefs_dict(_make_prefs_mock(None))
    assert result["send_at_hour"] == 8


# ── DigestPrefsUpdate Pydantic validation ─────────────────────────────────────

def test_digest_prefs_update_valid_frequency_daily():
    from routers.notification_digest import DigestPrefsUpdate
    model = DigestPrefsUpdate(frequency="daily")
    assert model.frequency == "daily"


def test_digest_prefs_update_valid_frequency_weekly():
    from routers.notification_digest import DigestPrefsUpdate
    model = DigestPrefsUpdate(frequency="weekly")
    assert model.frequency == "weekly"


def test_digest_prefs_update_invalid_frequency_raises():
    from routers.notification_digest import DigestPrefsUpdate
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        DigestPrefsUpdate(frequency="monthly")


def test_digest_prefs_update_send_at_hour_0_valid():
    """Boundary: send_at_hour=0 is valid."""
    from routers.notification_digest import DigestPrefsUpdate
    model = DigestPrefsUpdate(send_at_hour=0)
    assert model.send_at_hour == 0


def test_digest_prefs_update_send_at_hour_23_valid():
    """Boundary: send_at_hour=23 is valid."""
    from routers.notification_digest import DigestPrefsUpdate
    model = DigestPrefsUpdate(send_at_hour=23)
    assert model.send_at_hour == 23


def test_digest_prefs_update_send_at_hour_24_raises():
    from routers.notification_digest import DigestPrefsUpdate
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        DigestPrefsUpdate(send_at_hour=24)


def test_digest_prefs_update_send_at_hour_negative_raises():
    from routers.notification_digest import DigestPrefsUpdate
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        DigestPrefsUpdate(send_at_hour=-1)


def test_digest_prefs_update_all_none_model_dump():
    """All-None DigestPrefsUpdate → exclude_none dump returns empty dict."""
    from routers.notification_digest import DigestPrefsUpdate
    model = DigestPrefsUpdate()
    assert model.model_dump(exclude_none=True) == {}


# ── _DIGEST_DEFAULTS data integrity ──────────────────────────────────────────

def test_digest_defaults_all_expected_keys_present():
    from routers.notification_digest import _DIGEST_DEFAULTS
    expected = {
        "enabled", "frequency", "send_at_hour",
        "include_task_updates", "include_worker_activity", "include_credit_changes",
    }
    assert expected.issubset(set(_DIGEST_DEFAULTS.keys()))


def test_digest_defaults_enabled_is_true():
    from routers.notification_digest import _DIGEST_DEFAULTS
    assert _DIGEST_DEFAULTS["enabled"] is True


def test_digest_defaults_send_at_hour_in_range():
    from routers.notification_digest import _DIGEST_DEFAULTS
    hour = _DIGEST_DEFAULTS["send_at_hour"]
    assert isinstance(hour, int)
    assert 0 <= hour <= 23
