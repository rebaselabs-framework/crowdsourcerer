"""Comprehensive tests for the payouts router.

Covers all 6 endpoints:
  1. POST   /v1/payouts              — create payout request (worker only)
  2. GET    /v1/payouts              — list my payouts (pagination + status filter)
  3. GET    /v1/payouts/summary      — aggregate payout stats by status
  4. DELETE /v1/payouts/{payout_id}  — cancel pending payout (refunds credits)
  5. GET    /v1/payouts/admin/all    — admin list all payouts
  6. POST   /v1/payouts/{payout_id}/review — admin approve/reject payout
"""

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest
from httpx import AsyncClient, ASGITransport

USER_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ADMIN_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PAYOUT_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
NOW = datetime.now(timezone.utc)


def _token(user_id: str) -> str:
    from core.auth import create_access_token
    return create_access_token(user_id)


def _make_mock_db():
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.close = AsyncMock()
    db.execute = AsyncMock()
    db.scalar = AsyncMock(return_value=0)
    db.get = AsyncMock(return_value=None)

    async def _refresh(obj):
        pass
    db.refresh = _refresh

    async def _delete(obj):
        pass
    db.delete = _delete
    return db


def _db_override(mock_db):
    async def _inner():
        yield mock_db
    return _inner


def _scalar(value):
    r = MagicMock()
    r.scalar_one_or_none = MagicMock(return_value=value)
    r.scalar_one = MagicMock(return_value=value if not isinstance(value, MagicMock) else 0)
    r.scalar = MagicMock(return_value=value if not isinstance(value, MagicMock) else 0)
    r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    r.all = MagicMock(return_value=[])
    return r


def _make_user(
    user_id: str = USER_ID,
    role: str = "worker",
    credits: int = 5000,
) -> MagicMock:
    u = MagicMock()
    u.id = uuid.UUID(user_id)
    u.email = "worker@test.com"
    u.role = role
    u.credits = credits
    u.name = "Test Worker"
    u.is_banned = False
    u.is_admin = False
    u.totp_enabled = False
    u.plan = "free"
    return u


def _make_payout(
    payout_id: str = PAYOUT_ID,
    worker_id: str = USER_ID,
    credits_requested: int = 2000,
    usd_amount: float = 20.0,
    status: str = "pending",
    payout_method: str = "paypal",
    payout_details: dict = None,
    admin_note: str = None,
    processed_at: datetime = None,
) -> MagicMock:
    p = MagicMock()
    p.id = uuid.UUID(payout_id)
    p.worker_id = uuid.UUID(worker_id)
    p.credits_requested = credits_requested
    p.usd_amount = usd_amount
    p.status = status
    p.payout_method = payout_method
    p.payout_details = payout_details or {"email": "worker@paypal.com"}
    p.admin_note = admin_note
    p.processed_at = processed_at
    p.created_at = NOW
    p.updated_at = NOW
    return p


@pytest.fixture
async def client():
    from main import app
    from core.database import get_db
    from core.auth import get_current_user_id

    mock_db = _make_mock_db()
    app.dependency_overrides[get_db] = _db_override(mock_db)
    app.dependency_overrides[get_current_user_id] = lambda: USER_ID

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.mock_db = mock_db  # type: ignore[attr-defined]
        yield c

    app.dependency_overrides.clear()


@pytest.fixture
async def admin_client():
    from main import app
    from core.database import get_db
    from core.auth import get_current_user_id, require_admin

    mock_db = _make_mock_db()
    app.dependency_overrides[get_db] = _db_override(mock_db)
    app.dependency_overrides[get_current_user_id] = lambda: ADMIN_ID
    app.dependency_overrides[require_admin] = lambda: ADMIN_ID

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        c.mock_db = mock_db  # type: ignore[attr-defined]
        yield c

    app.dependency_overrides.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# 1. POST /v1/payouts — create payout request
# ═══════════════════════════════════════════════════════════════════════════════


class TestCreatePayout:

    @pytest.mark.asyncio
    async def test_create_payout_paypal_success(self, client):
        """Happy path: worker creates a PayPal payout request."""
        user = _make_user(credits=5000)
        payout = _make_payout()

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # select(UserDB).with_for_update()
                return _scalar(user)
            if call_count == 2:
                # check existing pending payout
                return _scalar(None)
            return _scalar(None)

        client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout):
            r = await client.post(
                "/v1/payouts",
                json={
                    "credits_requested": 2000,
                    "payout_method": "paypal",
                    "payout_details": {"email": "worker@paypal.com"},
                },
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )

        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_create_payout_bank_transfer_success(self, client):
        """Happy path: worker creates a bank transfer payout request."""
        user = _make_user(credits=5000)
        payout = _make_payout(payout_method="bank_transfer", payout_details={"account_name": "John", "iban": "DE89..."})

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(user)
            if call_count == 2:
                return _scalar(None)
            return _scalar(None)

        client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout):
            r = await client.post(
                "/v1/payouts",
                json={
                    "credits_requested": 2000,
                    "payout_method": "bank_transfer",
                    "payout_details": {"account_name": "John", "iban": "DE89..."},
                },
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )

        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_create_payout_crypto_success(self, client):
        """Happy path: worker creates a crypto payout request."""
        user = _make_user(credits=5000)
        payout = _make_payout(payout_method="crypto", payout_details={"network": "ethereum", "address": "0xabc"})

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(user)
            if call_count == 2:
                return _scalar(None)
            return _scalar(None)

        client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout):
            r = await client.post(
                "/v1/payouts",
                json={
                    "credits_requested": 2000,
                    "payout_method": "crypto",
                    "payout_details": {"network": "ethereum", "address": "0xabc"},
                },
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )

        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_create_payout_invalid_method_422(self, client):
        """Invalid payout_method is rejected by Pydantic schema (422)."""
        r = await client.post(
            "/v1/payouts",
            json={
                "credits_requested": 2000,
                "payout_method": "venmo",
                "payout_details": {"email": "x@y.com"},
            },
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        # Pydantic Literal validation rejects before the router runs
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_create_payout_below_minimum_400(self, client):
        """Requesting fewer than 1000 credits returns 400."""
        r = await client.post(
            "/v1/payouts",
            json={
                "credits_requested": 500,
                "payout_method": "paypal",
                "payout_details": {"email": "x@y.com"},
            },
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 400
        assert "1000" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_create_payout_paypal_missing_email_400(self, client):
        """PayPal method without email in details returns 400."""
        user = _make_user(credits=5000)
        client.mock_db.execute = AsyncMock(return_value=_scalar(user))

        r = await client.post(
            "/v1/payouts",
            json={
                "credits_requested": 2000,
                "payout_method": "paypal",
                "payout_details": {"phone": "123"},
            },
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 400
        assert "email" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_payout_bank_missing_iban_400(self, client):
        """Bank transfer without iban returns 400."""
        user = _make_user(credits=5000)
        client.mock_db.execute = AsyncMock(return_value=_scalar(user))

        r = await client.post(
            "/v1/payouts",
            json={
                "credits_requested": 2000,
                "payout_method": "bank_transfer",
                "payout_details": {"account_name": "John"},
            },
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 400
        assert "iban" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_payout_bank_missing_account_name_400(self, client):
        """Bank transfer without account_name returns 400."""
        user = _make_user(credits=5000)
        client.mock_db.execute = AsyncMock(return_value=_scalar(user))

        r = await client.post(
            "/v1/payouts",
            json={
                "credits_requested": 2000,
                "payout_method": "bank_transfer",
                "payout_details": {"iban": "DE89..."},
            },
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 400
        assert "account_name" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_payout_crypto_missing_network_400(self, client):
        """Crypto method without network returns 400."""
        user = _make_user(credits=5000)
        client.mock_db.execute = AsyncMock(return_value=_scalar(user))

        r = await client.post(
            "/v1/payouts",
            json={
                "credits_requested": 2000,
                "payout_method": "crypto",
                "payout_details": {"address": "0xabc"},
            },
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 400
        assert "network" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_payout_crypto_missing_address_400(self, client):
        """Crypto method without address returns 400."""
        user = _make_user(credits=5000)
        client.mock_db.execute = AsyncMock(return_value=_scalar(user))

        r = await client.post(
            "/v1/payouts",
            json={
                "credits_requested": 2000,
                "payout_method": "crypto",
                "payout_details": {"network": "ethereum"},
            },
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 400
        assert "address" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_payout_user_not_found_404(self, client):
        """User not in database returns 404."""
        client.mock_db.execute = AsyncMock(return_value=_scalar(None))

        r = await client.post(
            "/v1/payouts",
            json={
                "credits_requested": 2000,
                "payout_method": "paypal",
                "payout_details": {"email": "x@y.com"},
            },
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_create_payout_requester_role_403(self, client):
        """Requester role (not worker/both) returns 403."""
        user = _make_user(role="requester")
        client.mock_db.execute = AsyncMock(return_value=_scalar(user))

        r = await client.post(
            "/v1/payouts",
            json={
                "credits_requested": 2000,
                "payout_method": "paypal",
                "payout_details": {"email": "x@y.com"},
            },
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 403
        assert "worker" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_payout_insufficient_credits_400(self, client):
        """Not enough credits returns 400."""
        user = _make_user(credits=500)
        client.mock_db.execute = AsyncMock(return_value=_scalar(user))

        r = await client.post(
            "/v1/payouts",
            json={
                "credits_requested": 2000,
                "payout_method": "paypal",
                "payout_details": {"email": "x@y.com"},
            },
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 400
        assert "insufficient" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_payout_existing_pending_409(self, client):
        """User with an existing pending/processing payout returns 409."""
        user = _make_user(credits=5000)
        existing_payout = _make_payout(status="pending")

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(user)
            if call_count == 2:
                return _scalar(existing_payout)
            return _scalar(None)

        client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        r = await client.post(
            "/v1/payouts",
            json={
                "credits_requested": 2000,
                "payout_method": "paypal",
                "payout_details": {"email": "x@y.com"},
            },
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 409
        assert "pending" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_create_payout_deducts_credits(self, client):
        """Credits are deducted from user.credits on successful creation."""
        user = _make_user(credits=5000)
        payout = _make_payout()

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(user)
            if call_count == 2:
                return _scalar(None)
            return _scalar(None)

        client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout):
            r = await client.post(
                "/v1/payouts",
                json={
                    "credits_requested": 2000,
                    "payout_method": "paypal",
                    "payout_details": {"email": "x@y.com"},
                },
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )

        assert r.status_code == 200
        assert user.credits == 3000  # 5000 - 2000

    @pytest.mark.asyncio
    async def test_create_payout_both_role_allowed(self, client):
        """Users with role='both' can also request payouts."""
        user = _make_user(role="both", credits=5000)
        payout = _make_payout()

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(user)
            if call_count == 2:
                return _scalar(None)
            return _scalar(None)

        client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout):
            r = await client.post(
                "/v1/payouts",
                json={
                    "credits_requested": 1000,
                    "payout_method": "paypal",
                    "payout_details": {"email": "x@y.com"},
                },
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )

        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 2. GET /v1/payouts — list my payouts
# ═══════════════════════════════════════════════════════════════════════════════


class TestListMyPayouts:

    @pytest.mark.asyncio
    async def test_list_payouts_empty(self, client):
        """Empty payout list returns 200 with empty items."""
        total_result = MagicMock()
        total_result.scalar_one = MagicMock(return_value=0)

        items_result = MagicMock()
        items_result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return total_result
            return items_result

        client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        r = await client.get(
            "/v1/payouts",
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_list_payouts_with_status_filter(self, client):
        """Passing status= query param does not error (filtering logic)."""
        total_result = MagicMock()
        total_result.scalar_one = MagicMock(return_value=0)

        items_result = MagicMock()
        items_result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return total_result
            return items_result

        client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        r = await client.get(
            "/v1/payouts?status=pending",
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_list_payouts_invalid_status_422(self, client):
        """Invalid status filter returns 422."""
        r = await client.get(
            "/v1/payouts?status=bogus",
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_list_payouts_pagination_params(self, client):
        """Custom page and page_size are accepted."""
        total_result = MagicMock()
        total_result.scalar_one = MagicMock(return_value=0)

        items_result = MagicMock()
        items_result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return total_result
            return items_result

        client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        r = await client.get(
            "/v1/payouts?page=2&page_size=5",
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════════
# 3. GET /v1/payouts/summary — payout summary
# ═══════════════════════════════════════════════════════════════════════════════


class TestPayoutSummary:

    @pytest.mark.asyncio
    async def test_summary_empty(self, client):
        """Summary with no payouts returns zeros."""
        result = MagicMock()
        result.all = MagicMock(return_value=[])

        client.mock_db.execute = AsyncMock(return_value=result)

        r = await client.get(
            "/v1/payouts/summary",
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total_paid_usd"] == 0.0
        assert data["total_pending_usd"] == 0.0
        assert data["total_payouts"] == 0
        assert data["by_status"] == {}

    @pytest.mark.asyncio
    async def test_summary_with_data(self, client):
        """Summary correctly aggregates rows by status."""
        row_pending = MagicMock()
        row_pending.status = "pending"
        row_pending.cnt = 2
        row_pending.usd_total = 30.0
        row_pending.credits_total = 3000

        row_paid = MagicMock()
        row_paid.status = "paid"
        row_paid.cnt = 5
        row_paid.usd_total = 100.0
        row_paid.credits_total = 10000

        result = MagicMock()
        result.all = MagicMock(return_value=[row_pending, row_paid])

        client.mock_db.execute = AsyncMock(return_value=result)

        r = await client.get(
            "/v1/payouts/summary",
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total_paid_usd"] == 100.0
        assert data["total_pending_usd"] == 30.0
        assert data["total_payouts"] == 7
        assert data["by_status"]["pending"]["count"] == 2
        assert data["by_status"]["paid"]["usd_total"] == 100.0


# ═══════════════════════════════════════════════════════════════════════════════
# 4. DELETE /v1/payouts/{payout_id} — cancel pending payout
# ═══════════════════════════════════════════════════════════════════════════════


class TestCancelPayout:

    @pytest.mark.asyncio
    async def test_cancel_pending_payout_204(self, client):
        """Cancelling a pending payout returns 204 and refunds credits."""
        payout = _make_payout(status="pending", credits_requested=2000)
        user = _make_user(credits=3000)

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # select payout with_for_update
                return _scalar(payout)
            if call_count == 2:
                # select user with_for_update
                return MagicMock(scalar_one=MagicMock(return_value=user))
            return _scalar(None)

        client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        with patch("routers.payouts.create_notification", new_callable=AsyncMock):
            r = await client.delete(
                f"/v1/payouts/{PAYOUT_ID}",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )

        assert r.status_code == 204
        assert payout.status == "rejected"
        assert user.credits == 5000  # 3000 + 2000 refund

    @pytest.mark.asyncio
    async def test_cancel_payout_not_found_404(self, client):
        """Cancelling a nonexistent payout returns 404."""
        client.mock_db.execute = AsyncMock(return_value=_scalar(None))

        r = await client.delete(
            f"/v1/payouts/{PAYOUT_ID}",
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_cancel_processing_payout_409(self, client):
        """Cancelling a payout with status 'processing' returns 409."""
        payout = _make_payout(status="processing")
        client.mock_db.execute = AsyncMock(return_value=_scalar(payout))

        r = await client.delete(
            f"/v1/payouts/{PAYOUT_ID}",
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 409
        assert "processing" in r.json()["detail"]

    @pytest.mark.asyncio
    async def test_cancel_paid_payout_409(self, client):
        """Cancelling a paid payout returns 409."""
        payout = _make_payout(status="paid")
        client.mock_db.execute = AsyncMock(return_value=_scalar(payout))

        r = await client.delete(
            f"/v1/payouts/{PAYOUT_ID}",
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_cancel_rejected_payout_409(self, client):
        """Cancelling a rejected payout returns 409."""
        payout = _make_payout(status="rejected")
        client.mock_db.execute = AsyncMock(return_value=_scalar(payout))

        r = await client.delete(
            f"/v1/payouts/{PAYOUT_ID}",
            headers={"Authorization": f"Bearer {_token(USER_ID)}"},
        )
        assert r.status_code == 409

    @pytest.mark.asyncio
    async def test_cancel_payout_sets_admin_note(self, client):
        """Cancelled payout has admin_note set to 'Cancelled by worker'."""
        payout = _make_payout(status="pending", credits_requested=1000)
        user = _make_user(credits=0)

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(payout)
            if call_count == 2:
                return MagicMock(scalar_one=MagicMock(return_value=user))
            return _scalar(None)

        client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        with patch("routers.payouts.create_notification", new_callable=AsyncMock):
            r = await client.delete(
                f"/v1/payouts/{PAYOUT_ID}",
                headers={"Authorization": f"Bearer {_token(USER_ID)}"},
            )

        assert r.status_code == 204
        assert payout.admin_note == "Cancelled by worker"


# ═══════════════════════════════════════════════════════════════════════════════
# 5. GET /v1/payouts/admin/all — admin list all payouts
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdminListPayouts:

    @pytest.mark.asyncio
    async def test_admin_list_payouts_empty(self, admin_client):
        """Admin listing with no payouts returns 200 with empty items."""
        total_result = MagicMock()
        total_result.scalar_one = MagicMock(return_value=0)

        items_result = MagicMock()
        items_result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return total_result
            return items_result

        admin_client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        r = await admin_client.get(
            "/v1/payouts/admin/all",
            headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["items"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_admin_list_payouts_status_filter(self, admin_client):
        """Admin can filter payouts by status."""
        total_result = MagicMock()
        total_result.scalar_one = MagicMock(return_value=0)

        items_result = MagicMock()
        items_result.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return total_result
            return items_result

        admin_client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        r = await admin_client.get(
            "/v1/payouts/admin/all?status=pending",
            headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
        )
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_admin_list_requires_admin(self, client):
        """Non-admin user cannot access admin list endpoint.

        The client fixture does not override require_admin, so the
        actual dependency runs and should fail (no real DB user).
        We test this by using a dedicated client that lacks the
        require_admin override.
        """
        from main import app
        from core.database import get_db
        from core.auth import get_current_user_id, require_admin

        mock_db = _make_mock_db()
        # Simulate require_admin raising 403
        async def _deny_admin():
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="Admin access required")

        app.dependency_overrides[get_db] = _db_override(mock_db)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID
        app.dependency_overrides[require_admin] = _deny_admin

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.get(
                    "/v1/payouts/admin/all",
                    headers={"Authorization": f"Bearer {_token(USER_ID)}"},
                )
            assert r.status_code == 403
        finally:
            app.dependency_overrides.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. POST /v1/payouts/{payout_id}/review — admin approve/reject
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdminReviewPayout:

    @pytest.mark.asyncio
    async def test_review_approve_processing(self, admin_client):
        """Admin moves payout from pending to processing."""
        payout = _make_payout(status="pending")

        admin_client.mock_db.execute = AsyncMock(return_value=_scalar(payout))

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout), \
             patch("routers.payouts.create_notification", new_callable=AsyncMock), \
             patch("routers.payouts.log_admin_action", new_callable=AsyncMock):
            r = await admin_client.post(
                f"/v1/payouts/{PAYOUT_ID}/review",
                json={"status": "processing"},
                headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
            )

        assert r.status_code == 200
        assert payout.status == "processing"

    @pytest.mark.asyncio
    async def test_review_approve_paid(self, admin_client):
        """Admin moves payout from processing to paid."""
        payout = _make_payout(status="processing")

        admin_client.mock_db.execute = AsyncMock(return_value=_scalar(payout))

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout), \
             patch("routers.payouts.create_notification", new_callable=AsyncMock), \
             patch("routers.payouts.log_admin_action", new_callable=AsyncMock):
            r = await admin_client.post(
                f"/v1/payouts/{PAYOUT_ID}/review",
                json={"status": "paid"},
                headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
            )

        assert r.status_code == 200
        assert payout.status == "paid"

    @pytest.mark.asyncio
    async def test_review_reject_pending_refunds_credits(self, admin_client):
        """Admin rejects a pending payout, credits are refunded to worker."""
        payout = _make_payout(status="pending", credits_requested=3000)
        user = _make_user(credits=1000)

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # select payout with_for_update
                return _scalar(payout)
            if call_count == 2:
                # select user for refund
                return MagicMock(scalar_one=MagicMock(return_value=user))
            return _scalar(None)

        admin_client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout), \
             patch("routers.payouts.create_notification", new_callable=AsyncMock), \
             patch("routers.payouts.log_admin_action", new_callable=AsyncMock):
            r = await admin_client.post(
                f"/v1/payouts/{PAYOUT_ID}/review",
                json={"status": "rejected", "admin_note": "Invalid PayPal address"},
                headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
            )

        assert r.status_code == 200
        assert payout.status == "rejected"
        assert user.credits == 4000  # 1000 + 3000 refund
        assert payout.admin_note == "Invalid PayPal address"

    @pytest.mark.asyncio
    async def test_review_reject_processing_refunds_credits(self, admin_client):
        """Admin rejects a processing payout, credits are refunded."""
        payout = _make_payout(status="processing", credits_requested=5000)
        user = _make_user(credits=0)

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(payout)
            if call_count == 2:
                return MagicMock(scalar_one=MagicMock(return_value=user))
            return _scalar(None)

        admin_client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout), \
             patch("routers.payouts.create_notification", new_callable=AsyncMock), \
             patch("routers.payouts.log_admin_action", new_callable=AsyncMock):
            r = await admin_client.post(
                f"/v1/payouts/{PAYOUT_ID}/review",
                json={"status": "rejected"},
                headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
            )

        assert r.status_code == 200
        assert user.credits == 5000  # 0 + 5000 refund

    @pytest.mark.asyncio
    async def test_review_invalid_status_400(self, admin_client):
        """Invalid target status returns 400."""
        r = await admin_client.post(
            f"/v1/payouts/{PAYOUT_ID}/review",
            json={"status": "pending"},
            headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_review_not_found_404(self, admin_client):
        """Reviewing nonexistent payout returns 404."""
        admin_client.mock_db.execute = AsyncMock(return_value=_scalar(None))

        with patch("routers.payouts.log_admin_action", new_callable=AsyncMock):
            r = await admin_client.post(
                f"/v1/payouts/{PAYOUT_ID}/review",
                json={"status": "processing"},
                headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
            )
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_review_already_paid_409(self, admin_client):
        """Reviewing an already-paid payout returns 409."""
        payout = _make_payout(status="paid")
        admin_client.mock_db.execute = AsyncMock(return_value=_scalar(payout))

        with patch("routers.payouts.log_admin_action", new_callable=AsyncMock):
            r = await admin_client.post(
                f"/v1/payouts/{PAYOUT_ID}/review",
                json={"status": "processing"},
                headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
            )
        assert r.status_code == 409
        assert "paid" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_review_already_rejected_409(self, admin_client):
        """Reviewing an already-rejected payout returns 409."""
        payout = _make_payout(status="rejected")
        admin_client.mock_db.execute = AsyncMock(return_value=_scalar(payout))

        with patch("routers.payouts.log_admin_action", new_callable=AsyncMock):
            r = await admin_client.post(
                f"/v1/payouts/{PAYOUT_ID}/review",
                json={"status": "paid"},
                headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
            )
        assert r.status_code == 409
        assert "rejected" in r.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_review_sets_processed_at(self, admin_client):
        """Review sets processed_at timestamp on the payout."""
        payout = _make_payout(status="pending", processed_at=None)

        admin_client.mock_db.execute = AsyncMock(return_value=_scalar(payout))

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout), \
             patch("routers.payouts.create_notification", new_callable=AsyncMock), \
             patch("routers.payouts.log_admin_action", new_callable=AsyncMock):
            r = await admin_client.post(
                f"/v1/payouts/{PAYOUT_ID}/review",
                json={"status": "processing"},
                headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
            )

        assert r.status_code == 200
        assert payout.processed_at is not None

    @pytest.mark.asyncio
    async def test_review_requires_admin(self):
        """Non-admin user cannot review payouts."""
        from main import app
        from core.database import get_db
        from core.auth import get_current_user_id, require_admin

        mock_db = _make_mock_db()

        async def _deny_admin():
            from fastapi import HTTPException
            raise HTTPException(status_code=403, detail="Admin access required")

        app.dependency_overrides[get_db] = _db_override(mock_db)
        app.dependency_overrides[get_current_user_id] = lambda: USER_ID
        app.dependency_overrides[require_admin] = _deny_admin

        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                r = await c.post(
                    f"/v1/payouts/{PAYOUT_ID}/review",
                    json={"status": "processing"},
                    headers={"Authorization": f"Bearer {_token(USER_ID)}"},
                )
            assert r.status_code == 403
        finally:
            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_review_creates_notification_on_processing(self, admin_client):
        """Moving to 'processing' creates a notification for the worker."""
        payout = _make_payout(status="pending")

        admin_client.mock_db.execute = AsyncMock(return_value=_scalar(payout))

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout), \
             patch("routers.payouts.create_notification", new_callable=AsyncMock) as mock_notify, \
             patch("routers.payouts.log_admin_action", new_callable=AsyncMock):
            r = await admin_client.post(
                f"/v1/payouts/{PAYOUT_ID}/review",
                json={"status": "processing"},
                headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
            )

        assert r.status_code == 200
        assert mock_notify.called
        call_args = mock_notify.call_args
        # Positional args: db, worker_id, type, title, body
        assert call_args[0][2] == "payout_processing"

    @pytest.mark.asyncio
    async def test_review_creates_notification_on_paid(self, admin_client):
        """Moving to 'paid' creates a notification for the worker."""
        payout = _make_payout(status="processing")

        admin_client.mock_db.execute = AsyncMock(return_value=_scalar(payout))

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout), \
             patch("routers.payouts.create_notification", new_callable=AsyncMock) as mock_notify, \
             patch("routers.payouts.log_admin_action", new_callable=AsyncMock):
            r = await admin_client.post(
                f"/v1/payouts/{PAYOUT_ID}/review",
                json={"status": "paid"},
                headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
            )

        assert r.status_code == 200
        assert mock_notify.called
        call_args = mock_notify.call_args
        assert call_args[0][2] == "payout_paid"

    @pytest.mark.asyncio
    async def test_review_creates_notification_on_rejected(self, admin_client):
        """Moving to 'rejected' creates a notification for the worker."""
        payout = _make_payout(status="pending", credits_requested=1000)
        user = _make_user(credits=0)

        call_count = 0

        async def _execute_side(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _scalar(payout)
            if call_count == 2:
                return MagicMock(scalar_one=MagicMock(return_value=user))
            return _scalar(None)

        admin_client.mock_db.execute = AsyncMock(side_effect=_execute_side)

        with patch("routers.payouts.PayoutRequestOut.model_validate", return_value=payout), \
             patch("routers.payouts.create_notification", new_callable=AsyncMock) as mock_notify, \
             patch("routers.payouts.log_admin_action", new_callable=AsyncMock):
            r = await admin_client.post(
                f"/v1/payouts/{PAYOUT_ID}/review",
                json={"status": "rejected", "admin_note": "Bad details"},
                headers={"Authorization": f"Bearer {_token(ADMIN_ID)}"},
            )

        assert r.status_code == 200
        assert mock_notify.called
        call_args = mock_notify.call_args
        assert call_args[0][2] == "payout_rejected"
