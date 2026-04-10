"""Tests for webhook signature verification."""
import hashlib
import hmac
import time

import pytest

from crowdsourcerer import verify_webhook, verify_webhook_with_rotation


SECRET = "test_secret_abc123"
PAYLOAD = b'{"task_id":"abc","event":"task.completed"}'


def _sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Generate a valid signature header."""
    ts = str(timestamp or int(time.time()))
    sig_input = f"{ts}.".encode() + payload
    sig = hmac.new(secret.encode(), sig_input, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _sign_dual(
    payload: bytes,
    new_secret: str,
    old_secret: str,
    timestamp: int | None = None,
) -> str:
    """Generate a dual-signature header (rotation mode)."""
    ts = str(timestamp or int(time.time()))
    sig_input = f"{ts}.".encode() + payload
    v1 = hmac.new(new_secret.encode(), sig_input, hashlib.sha256).hexdigest()
    v0 = hmac.new(old_secret.encode(), sig_input, hashlib.sha256).hexdigest()
    return f"t={ts},v1={v1},v0={v0}"


# ─── verify_webhook ──────────────────────────────────────────────────────


class TestVerifyWebhook:
    def test_valid_signature(self):
        sig = _sign(PAYLOAD, SECRET)
        assert verify_webhook(PAYLOAD, SECRET, sig) is True

    def test_invalid_signature(self):
        sig = _sign(PAYLOAD, SECRET)
        assert verify_webhook(PAYLOAD, "wrong_secret", sig) is False

    def test_tampered_payload(self):
        sig = _sign(PAYLOAD, SECRET)
        assert verify_webhook(b"tampered", SECRET, sig) is False

    def test_expired_timestamp(self):
        old_ts = int(time.time()) - 600  # 10 minutes ago
        sig = _sign(PAYLOAD, SECRET, timestamp=old_ts)
        assert verify_webhook(PAYLOAD, SECRET, sig) is False

    def test_expired_but_tolerance_disabled(self):
        old_ts = int(time.time()) - 600
        sig = _sign(PAYLOAD, SECRET, timestamp=old_ts)
        assert verify_webhook(PAYLOAD, SECRET, sig, tolerance=0) is True

    def test_custom_tolerance(self):
        old_ts = int(time.time()) - 60  # 1 minute ago
        sig = _sign(PAYLOAD, SECRET, timestamp=old_ts)
        # 30s tolerance should reject
        assert verify_webhook(PAYLOAD, SECRET, sig, tolerance=30) is False
        # 120s tolerance should accept
        assert verify_webhook(PAYLOAD, SECRET, sig, tolerance=120) is True

    def test_missing_timestamp(self):
        assert verify_webhook(PAYLOAD, SECRET, "v1=abc123") is False

    def test_missing_v1(self):
        assert verify_webhook(PAYLOAD, SECRET, "t=12345") is False

    def test_empty_header(self):
        assert verify_webhook(PAYLOAD, SECRET, "") is False

    def test_garbage_header(self):
        assert verify_webhook(PAYLOAD, SECRET, "garbage") is False

    def test_payload_must_be_bytes(self):
        sig = _sign(PAYLOAD, SECRET)
        with pytest.raises(ValueError, match="bytes"):
            verify_webhook("not bytes", SECRET, sig)  # type: ignore[arg-type]

    def test_secret_must_not_be_empty(self):
        sig = _sign(PAYLOAD, SECRET)
        with pytest.raises(ValueError, match="empty"):
            verify_webhook(PAYLOAD, "", sig)


# ─── verify_webhook_with_rotation ────────────────────────────────────────


class TestVerifyWithRotation:
    def test_current_secret_v1(self):
        new = "new_secret"
        old = "old_secret"
        sig = _sign_dual(PAYLOAD, new, old)
        assert verify_webhook_with_rotation(PAYLOAD, new, old, sig) is True

    def test_previous_secret_v0(self):
        new = "new_secret"
        old = "old_secret"
        sig = _sign_dual(PAYLOAD, new, old)
        # Swap: pretend the developer hasn't updated to new secret yet
        # So current_secret = old, previous_secret = None — v1 won't match
        # But if we pass old as previous, v0 should match
        assert verify_webhook_with_rotation(PAYLOAD, "totally_wrong", old, sig) is True

    def test_no_previous_secret(self):
        sig = _sign(PAYLOAD, SECRET)
        assert verify_webhook_with_rotation(PAYLOAD, SECRET, None, sig) is True

    def test_both_wrong(self):
        sig = _sign_dual(PAYLOAD, "new", "old")
        assert verify_webhook_with_rotation(PAYLOAD, "wrong1", "wrong2", sig) is False
