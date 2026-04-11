"""Shared pytest configuration for the CrowdSorcerer API test suite.

Adds the project root (apps/api/) to sys.path so that test files can import
`main`, `core`, `models`, `routers`, and `workers` without needing an
installed package or a manual PYTHONPATH setting.

Also provides shared test helpers used across 10+ test files:
- real_token() — create a valid JWT for test auth
- db_override() — FastAPI dependency override for mock DB sessions
"""
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# Add apps/api/ to sys.path (parent of the tests/ directory)
api_root = Path(__file__).parent.parent
if str(api_root) not in sys.path:
    sys.path.insert(0, str(api_root))


@pytest.fixture(autouse=True)
def _open_registration_for_tests():
    """Tests predate the production registration gate — flip it on for
    every test so the 19+ existing /register tests still exercise the
    signup path. Tests that want to verify the closed-registration
    behaviour override this by setting the flag back to False locally.
    """
    from core.config import get_settings
    s = get_settings()
    prev = s.registration_enabled
    s.registration_enabled = True
    try:
        yield
    finally:
        s.registration_enabled = prev


# ─── Shared test helpers ──────────────────────────────────────────────────
# Use these instead of defining local copies in each test file.
# Import: from conftest import real_token, db_override


def real_token(user_id: str | None = None, tv: int = 0) -> str:
    """Create a valid JWT access token for testing.

    Args:
        user_id: UUID string. Auto-generated if None.
        tv: Token version (for token invalidation tests).
    """
    from core.auth import create_access_token
    return create_access_token(user_id or str(uuid.uuid4()), token_version=tv)


def db_override(mock_db):
    """Return an async-generator function for FastAPI dependency override.

    Usage:
        db = AsyncMock()
        app.dependency_overrides[get_db] = db_override(db)
    """
    async def _override():
        yield mock_db
    return _override
