"""
Test that the OpenAPI schema generates cleanly.

This is a regression test for issues that caused /v1/openapi.json → 500:
  1. Duplicate Pydantic model names across different router files
  2. Forward reference strings in route signatures not resolvable at module level

If app.openapi() throws, new routes/models likely introduced a conflict.
"""
import os
import pytest


@pytest.fixture
def app():
    os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://fake:fake@localhost/fake")
    os.environ.setdefault("JWT_SECRET", "test-secret")
    os.environ.setdefault("API_KEY_SALT", "test-salt")
    os.environ.setdefault("DEBUG", "false")
    from main import app
    return app


def test_openapi_schema_generates_cleanly(app):
    """
    app.openapi() must not raise an exception.

    Previous failures:
    - PydanticUserError: duplicate model names (BanWorkerRequest, UnreadCountOut)
    - PydanticUserError: forward reference 'BulkInviteRequest' not resolvable
    """
    schema = app.openapi()
    assert schema is not None
    assert "paths" in schema
    assert "components" in schema
    assert "schemas" in schema["components"]

    paths = schema["paths"]
    schemas = schema["components"]["schemas"]

    # Must have a reasonable number of paths
    assert len(paths) > 100, f"Expected >100 paths, got {len(paths)}"

    # Must have a reasonable number of schemas
    assert len(schemas) > 50, f"Expected >50 schemas, got {len(schemas)}"


def test_openapi_no_duplicate_schema_names(app):
    """
    All component schema names must be unique.
    Pydantic v2 deduplicates by adding numeric suffixes, but if two different
    models share the same name, it indicates a model naming conflict.
    """
    schema = app.openapi()
    schema_names = list(schema["components"]["schemas"].keys())
    base_names = [n.rstrip("0123456789").rstrip() for n in schema_names]

    # Check for names that appear multiple times (with numeric suffixes)
    from collections import Counter
    counts = Counter(base_names)
    duplicates = [name for name, count in counts.items() if count > 1]

    assert len(duplicates) == 0, (
        f"Found duplicate schema base names (model name conflicts): {duplicates}"
    )


def test_openapi_key_routes_present(app):
    """Ensure critical API routes are present in the schema."""
    schema = app.openapi()
    paths = set(schema["paths"].keys())

    required_paths = [
        "/v1/auth/register",
        "/v1/auth/token",
        "/v1/tasks",
        "/v1/platform/stats",
        "/v1/leaderboard",
        "/v1/webhooks/events",
        "/v1/scopes",
        "/openapi.json",
    ]

    for path in required_paths:
        assert path in paths, f"Required path missing from OpenAPI schema: {path}"
