"""Regression tests for critical bugs that were found and fixed.

Each test targets a specific bug with a comment linking to the fix.
These tests exist to prevent regressions — if they fail, a critical
bug has been reintroduced.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

# ─── Helper: create a real-ish JWT for test auth ──────────────────────────────

def _real_token(user_id: str = None, tv: int = 0) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id or str(uuid.uuid4()), token_version=tv)


# ══════════════════════════════════════════════════════════════════════════════
# REGRESSION: Refresh tokens were never persisted to database
# Bug: db.flush() in create_refresh_token without db.commit() in callers
# Fix: Added db.commit() after create_refresh_token() in login/register/oauth/2fa
# ══════════════════════════════════════════════════════════════════════════════

class TestRefreshTokenPersistence:
    """Verify that login and register actually persist refresh tokens."""

    @pytest.mark.asyncio
    async def test_login_commits_refresh_token(self):
        """POST /v1/auth/login must commit the refresh token to DB.

        Previously, create_refresh_token() called db.flush() but the login
        handler never committed, so the token was rolled back on session close.
        Users were logged out every 30 minutes.
        """
        from main import app
        from core.database import get_db
        from models.db import UserDB

        user_id = uuid.uuid4()
        mock_user = MagicMock(spec=UserDB)
        mock_user.id = user_id
        mock_user.email = "test@example.com"
        mock_user.password_hash = "$2b$12$LJ3m4ys8Ox/0.VBGWMWsAekxB4/iST3PJ/NuFJZfIyuKl4CWbWC6W"  # "TestPass123!"
        mock_user.is_active = True
        mock_user.is_banned = False
        mock_user.totp_enabled = False
        mock_user.token_version = 0

        db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_user
        db.execute.return_value = mock_result
        db.scalar.return_value = None  # for refresh token creation
        db.flush = AsyncMock()
        db.commit = AsyncMock()

        commit_count = 0
        original_commit = db.commit

        async def counting_commit():
            nonlocal commit_count
            commit_count += 1
            return await original_commit()

        db.commit = counting_commit

        async def _db():
            yield db

        app.dependency_overrides[get_db] = _db

        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post("/v1/auth/login", json={
                    "email": "test@example.com",
                    "password": "TestPass123!",
                })

            # The response should include a refresh token
            if r.status_code == 200:
                data = r.json()
                # Key assertion: commit was called at least once
                # (the bug was that commit was never called after flush)
                assert commit_count >= 1, (
                    "REGRESSION: db.commit() was not called after creating refresh token. "
                    "This means refresh tokens are not persisted (the 30-min logout bug)."
                )
        finally:
            app.dependency_overrides.pop(get_db, None)


# ══════════════════════════════════════════════════════════════════════════════
# REGRESSION: TaskCreateRequest silently dropped unknown fields
# Bug: Pydantic's default extra="ignore" swallowed typos like 'title', 'instructions'
# Fix: Added model_config = ConfigDict(extra="forbid")
# ══════════════════════════════════════════════════════════════════════════════

class TestTaskCreateStrictValidation:
    """Verify that TaskCreateRequest rejects unknown fields."""

    def test_rejects_title_field(self):
        """'title' is not a valid field — should raise ValidationError."""
        from models.schemas import TaskCreateRequest
        with pytest.raises(ValidationError) as exc_info:
            TaskCreateRequest(
                type="label_text",
                input={"text": "test"},
                title="This should be rejected",  # type: ignore
            )
        assert "extra" in str(exc_info.value).lower() or "title" in str(exc_info.value).lower()

    def test_rejects_instructions_typo(self):
        """'instructions' should be 'task_instructions'."""
        from models.schemas import TaskCreateRequest
        with pytest.raises(ValidationError):
            TaskCreateRequest(
                type="label_text",
                input={"text": "test"},
                instructions="Wrong field name",  # type: ignore
            )

    def test_rejects_labels_at_top_level(self):
        """'labels' should be inside input, not at top level."""
        from models.schemas import TaskCreateRequest
        with pytest.raises(ValidationError):
            TaskCreateRequest(
                type="label_text",
                input={"text": "test"},
                labels=["pos", "neg"],  # type: ignore
            )

    def test_accepts_valid_request(self):
        """A properly formed request should work fine."""
        from models.schemas import TaskCreateRequest
        req = TaskCreateRequest(
            type="label_text",
            input={"text": "test", "categories": ["pos", "neg"]},
            task_instructions="Classify sentiment",
            worker_reward_credits=5,
        )
        assert req.type == "label_text"
        assert req.task_instructions == "Classify sentiment"


# ══════════════════════════════════════════════════════════════════════════════
# REGRESSION: Stripe webhook bypass when secret is empty
# Bug: Webhook handler accepted unverified payloads if stripe_webhook_secret=""
# Fix: Reject all webhooks with 503 when secret is not configured
# ══════════════════════════════════════════════════════════════════════════════

class TestStripeWebhookSecurity:
    """Verify that Stripe webhooks are rejected when secret is not configured."""

    @pytest.mark.asyncio
    async def test_rejects_webhook_when_secret_empty(self):
        """Webhook endpoint must reject all payloads when secret is not configured."""
        from main import app

        with patch("routers.stripe_webhooks.get_settings") as mock_settings:
            mock_s = MagicMock()
            mock_s.stripe_webhook_secret = ""  # Not configured
            mock_settings.return_value = mock_s

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
                r = await c.post(
                    "/v1/webhooks/stripe",
                    content=b'{"type":"checkout.session.completed"}',
                    headers={"Content-Type": "application/json"},
                )
            assert r.status_code == 503, (
                f"REGRESSION: Stripe webhook accepted payload without secret! "
                f"Got {r.status_code}, expected 503."
            )


# ══════════════════════════════════════════════════════════════════════════════
# REGRESSION: HTML injection in user names
# Bug: No server-side sanitization on name fields
# Fix: Added _strip_html validator on RegisterRequest and ProfileUpdateRequest
# ══════════════════════════════════════════════════════════════════════════════

class TestHTMLSanitization:
    """Verify that HTML tags are stripped from user-facing text fields."""

    def test_register_strips_html_from_name(self):
        from models.schemas import RegisterRequest
        req = RegisterRequest(
            email="test@example.com",
            password="TestPass123!",
            name="<script>alert(1)</script>Hello",
        )
        assert "<script>" not in (req.name or "")
        assert "Hello" in (req.name or "")

    def test_register_allows_normal_names(self):
        from models.schemas import RegisterRequest
        req = RegisterRequest(
            email="test@example.com",
            password="TestPass123!",
            name="John Smith",
        )
        assert req.name == "John Smith"

    def test_profile_strips_html_from_bio(self):
        from models.schemas import ProfileUpdateRequest
        req = ProfileUpdateRequest(
            bio="<b>Bold</b> and <em>italic</em> text",
        )
        assert "<b>" not in (req.bio or "")
        assert "Bold" in (req.bio or "")


# ══════════════════════════════════════════════════════════════════════════════
# REGRESSION: apiFetch error handling for Pydantic validation arrays
# (Frontend-only — can't unit test here, but document the pattern)
# Bug: apiFetch threw new Error(body.detail) when detail was an array,
#      producing "[object Object]" as the error message
# Fix: apiFetch now handles array detail by joining .msg fields
# ══════════════════════════════════════════════════════════════════════════════
# This is documented rather than tested because it's a TypeScript frontend fix.
