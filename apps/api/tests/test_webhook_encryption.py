"""Tests for webhook secret encryption at rest.

Tests cover:
  - Encrypt/decrypt round-trip
  - Legacy plaintext passthrough
  - Invalid ciphertext detection
  - Rotation grace period (previous_secret + v0 signature)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestEncryptionRoundTrip:
    """Test encrypt_secret and decrypt_secret."""

    def test_encrypt_decrypt_roundtrip(self):
        from core.encryption import encrypt_secret, decrypt_secret

        secret = secrets.token_urlsafe(32)
        encrypted = encrypt_secret(secret)

        assert encrypted.startswith("enc:")
        assert encrypted != secret

        decrypted = decrypt_secret(encrypted)
        assert decrypted == secret

    def test_decrypt_legacy_plaintext(self):
        """Plaintext values (without enc: prefix) pass through unchanged."""
        from core.encryption import decrypt_secret

        plaintext = "legacy-plaintext-secret"
        result = decrypt_secret(plaintext)
        assert result == plaintext

    def test_decrypt_invalid_ciphertext_raises(self):
        """Invalid encrypted values should raise ValueError."""
        from core.encryption import decrypt_secret

        with pytest.raises(ValueError, match="Failed to decrypt"):
            decrypt_secret("enc:invalid-ciphertext-data")

    def test_encrypt_produces_different_ciphertexts(self):
        """Fernet encryption includes a timestamp — same plaintext gives different ciphertext."""
        from core.encryption import encrypt_secret

        secret = "test-secret"
        ct1 = encrypt_secret(secret)
        ct2 = encrypt_secret(secret)
        # Fernet includes a timestamp nonce, so ciphertexts differ
        assert ct1 != ct2

    def test_encrypted_prefix(self):
        from core.encryption import encrypt_secret, ENCRYPTED_PREFIX

        encrypted = encrypt_secret("test")
        assert encrypted.startswith(ENCRYPTED_PREFIX)


class TestSignatureWithEncryption:
    """Test that encrypted secrets work correctly for HMAC signing."""

    def test_hmac_signing_with_decrypted_secret(self):
        """Decrypted secret should produce valid HMAC signatures."""
        from core.encryption import encrypt_secret, decrypt_secret

        secret = secrets.token_urlsafe(32)
        encrypted = encrypt_secret(secret)

        payload = json.dumps({"event": "task.completed"}).encode()
        timestamp = str(int(time.time()))
        sig_input = f"{timestamp}.".encode() + payload

        # Sign with original secret
        expected_sig = hmac.new(secret.encode(), sig_input, hashlib.sha256).hexdigest()

        # Sign with decrypted secret
        decrypted = decrypt_secret(encrypted)
        actual_sig = hmac.new(decrypted.encode(), sig_input, hashlib.sha256).hexdigest()

        assert actual_sig == expected_sig


class TestRotationGracePeriod:
    """Test dual-signature (v0 + v1) during rotation grace period."""

    @pytest.mark.asyncio
    async def test_deliver_includes_v0_during_grace_period(self):
        """During rotation grace period, delivery should include both v1 and v0 signatures."""
        from core.encryption import encrypt_secret
        from core.webhooks import _deliver_to_endpoint

        old_secret = secrets.token_urlsafe(32)
        new_secret = secrets.token_urlsafe(32)

        mock_endpoint = MagicMock()
        mock_endpoint.id = uuid.uuid4()
        mock_endpoint.url = "https://example.com/webhook"
        mock_endpoint.secret = encrypt_secret(new_secret)
        mock_endpoint.previous_secret = encrypt_secret(old_secret)
        mock_endpoint.previous_secret_expires_at = datetime.now(timezone.utc) + timedelta(hours=23)
        mock_endpoint.events = None

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()

        with patch("core.webhooks._get_webhook_client", return_value=mock_client), \
             patch("core.webhooks.AsyncSessionLocal", return_value=mock_session), \
             patch("core.webhooks._get_user_event_template", return_value=None), \
             patch("core.webhooks.enqueue_retry", new_callable=AsyncMock):
            await _deliver_to_endpoint(
                endpoint=mock_endpoint,
                task_id=str(uuid.uuid4()),
                user_id=str(uuid.uuid4()),
                event_type="task.completed",
                extra=None,
                max_retries=3,
            )

        # Verify the POST was called
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")

        sig_header = headers["X-Crowdsorcerer-Signature"]
        # Should have both v1 and v0
        assert ",v1=" in sig_header
        assert ",v0=" in sig_header

    @pytest.mark.asyncio
    async def test_deliver_no_v0_after_grace_period(self):
        """After grace period expires, delivery should only include v1 signature."""
        from core.encryption import encrypt_secret
        from core.webhooks import _deliver_to_endpoint

        old_secret = secrets.token_urlsafe(32)
        new_secret = secrets.token_urlsafe(32)

        mock_endpoint = MagicMock()
        mock_endpoint.id = uuid.uuid4()
        mock_endpoint.url = "https://example.com/webhook"
        mock_endpoint.secret = encrypt_secret(new_secret)
        mock_endpoint.previous_secret = encrypt_secret(old_secret)
        # Expired 1 hour ago
        mock_endpoint.previous_secret_expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        mock_endpoint.events = None

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()

        with patch("core.webhooks._get_webhook_client", return_value=mock_client), \
             patch("core.webhooks.AsyncSessionLocal", return_value=mock_session), \
             patch("core.webhooks._get_user_event_template", return_value=None), \
             patch("core.webhooks.enqueue_retry", new_callable=AsyncMock):
            await _deliver_to_endpoint(
                endpoint=mock_endpoint,
                task_id=str(uuid.uuid4()),
                user_id=str(uuid.uuid4()),
                event_type="task.completed",
                extra=None,
                max_retries=3,
            )

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")

        sig_header = headers["X-Crowdsorcerer-Signature"]
        assert ",v1=" in sig_header
        assert ",v0=" not in sig_header

    @pytest.mark.asyncio
    async def test_deliver_no_v0_without_previous_secret(self):
        """Without previous_secret, delivery should only include v1 signature."""
        from core.encryption import encrypt_secret
        from core.webhooks import _deliver_to_endpoint

        new_secret = secrets.token_urlsafe(32)

        mock_endpoint = MagicMock()
        mock_endpoint.id = uuid.uuid4()
        mock_endpoint.url = "https://example.com/webhook"
        mock_endpoint.secret = encrypt_secret(new_secret)
        mock_endpoint.previous_secret = None
        mock_endpoint.previous_secret_expires_at = None
        mock_endpoint.events = None

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=None)
        mock_session.add = MagicMock()
        mock_session.commit = AsyncMock()
        mock_session.execute = AsyncMock()

        with patch("core.webhooks._get_webhook_client", return_value=mock_client), \
             patch("core.webhooks.AsyncSessionLocal", return_value=mock_session), \
             patch("core.webhooks._get_user_event_template", return_value=None), \
             patch("core.webhooks.enqueue_retry", new_callable=AsyncMock):
            await _deliver_to_endpoint(
                endpoint=mock_endpoint,
                task_id=str(uuid.uuid4()),
                user_id=str(uuid.uuid4()),
                event_type="task.completed",
                extra=None,
                max_retries=3,
            )

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")

        sig_header = headers["X-Crowdsorcerer-Signature"]
        assert ",v1=" in sig_header
        assert ",v0=" not in sig_header
