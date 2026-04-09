"""Tests for the system health monitoring and alerting module."""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")
os.environ.setdefault("DEBUG", "true")

import pytest

from core import system_alerts
from core.system_alerts import (
    record_http_error,
    record_sweep_success,
    record_sweep_error,
    check_and_fire_alerts,
    _is_cooled_down,
    _system_alert_html,
)


@pytest.fixture(autouse=True)
def reset_alert_state():
    """Ensure every test starts with a clean module-level state."""
    system_alerts._error_window.clear()
    system_alerts._sweep_consecutive_errors = 0
    system_alerts._alert_cooldowns.clear()
    yield


# ─── record_http_error ────────────────────────────────────────────────────────


def test_record_http_error_records_5xx():
    """5xx status codes are appended to the error window."""
    record_http_error("/v1/tasks", 500)
    record_http_error("/v1/tasks", 502)
    record_http_error("/v1/tasks", 503)

    assert len(system_alerts._error_window) == 3
    for ts, path in system_alerts._error_window:
        assert path == "/v1/tasks"
        assert isinstance(ts, datetime)


def test_record_http_error_ignores_4xx():
    """4xx status codes are NOT recorded."""
    record_http_error("/v1/tasks", 400)
    record_http_error("/v1/tasks", 403)
    record_http_error("/v1/tasks", 404)
    record_http_error("/v1/tasks", 429)

    assert len(system_alerts._error_window) == 0


def test_record_http_error_ignores_2xx():
    """Successful responses are NOT recorded."""
    record_http_error("/v1/tasks", 200)
    record_http_error("/v1/tasks", 201)
    record_http_error("/v1/tasks", 204)

    assert len(system_alerts._error_window) == 0


def test_record_http_error_ignores_3xx():
    """Redirect responses are NOT recorded."""
    record_http_error("/v1/tasks", 301)
    record_http_error("/v1/tasks", 302)

    assert len(system_alerts._error_window) == 0


def test_record_http_error_stores_correct_path():
    """Each error records the correct path."""
    record_http_error("/v1/tasks", 500)
    record_http_error("/v1/workers", 503)

    paths = [path for _, path in system_alerts._error_window]
    assert paths == ["/v1/tasks", "/v1/workers"]


def test_record_http_error_boundary_499():
    """Status code 499 (just below 500) is not recorded."""
    record_http_error("/v1/tasks", 499)
    assert len(system_alerts._error_window) == 0


def test_record_http_error_boundary_500():
    """Status code 500 (exactly the threshold) IS recorded."""
    record_http_error("/v1/tasks", 500)
    assert len(system_alerts._error_window) == 1


# ─── record_sweep_success / record_sweep_error ───────────────────────────────


def test_record_sweep_success_resets_counter():
    """record_sweep_success sets the consecutive error counter to zero."""
    system_alerts._sweep_consecutive_errors = 5
    record_sweep_success()
    assert system_alerts._sweep_consecutive_errors == 0


def test_record_sweep_error_increments_counter():
    """record_sweep_error increments the consecutive error counter."""
    assert system_alerts._sweep_consecutive_errors == 0
    record_sweep_error()
    assert system_alerts._sweep_consecutive_errors == 1
    record_sweep_error()
    assert system_alerts._sweep_consecutive_errors == 2


def test_record_sweep_error_then_success_resets():
    """Errors accumulate, but a single success resets them."""
    record_sweep_error()
    record_sweep_error()
    record_sweep_error()
    assert system_alerts._sweep_consecutive_errors == 3

    record_sweep_success()
    assert system_alerts._sweep_consecutive_errors == 0


# ─── _is_cooled_down ──────────────────────────────────────────────────────────


def test_is_cooled_down_no_prior_alert():
    """Returns True when there is no prior alert of this type."""
    now = datetime.now(timezone.utc)
    assert _is_cooled_down("error_rate_spike", now) is True


def test_is_cooled_down_within_cooldown_period():
    """Returns False when the last alert was within the cooldown period."""
    now = datetime.now(timezone.utc)
    system_alerts._alert_cooldowns["error_rate_spike"] = now - timedelta(minutes=30)
    # Default cooldown is 1 hour, so 30 minutes ago is still within cooldown
    assert _is_cooled_down("error_rate_spike", now) is False


def test_is_cooled_down_after_cooldown_period():
    """Returns True when the cooldown has fully elapsed."""
    now = datetime.now(timezone.utc)
    # Place the last alert 2 hours ago (cooldown default is 1 hour)
    system_alerts._alert_cooldowns["error_rate_spike"] = now - timedelta(hours=2)
    assert _is_cooled_down("error_rate_spike", now) is True


def test_is_cooled_down_exactly_at_boundary():
    """Returns True when exactly at the cooldown boundary (>= comparison)."""
    now = datetime.now(timezone.utc)
    system_alerts._alert_cooldowns["sweeper_stall"] = now - timedelta(hours=1)
    assert _is_cooled_down("sweeper_stall", now) is True


def test_is_cooled_down_independent_per_alert_type():
    """Cooldowns are tracked independently per alert type."""
    now = datetime.now(timezone.utc)
    system_alerts._alert_cooldowns["error_rate_spike"] = now - timedelta(minutes=10)
    # error_rate_spike is cooling down, but sweeper_stall has never fired
    assert _is_cooled_down("error_rate_spike", now) is False
    assert _is_cooled_down("sweeper_stall", now) is True


# ─── _system_alert_html ──────────────────────────────────────────────────────


def test_system_alert_html_critical_color():
    """Critical alerts use the red color (#ef4444)."""
    html = _system_alert_html(
        alert_type="error_rate_spike",
        severity="critical",
        title="Error rate spike: 15 errors in 5m",
        detail={"error_count": 15, "window_minutes": 5},
    )
    assert "#ef4444" in html
    assert "CRITICAL" in html


def test_system_alert_html_warning_color():
    """Warning alerts use the amber color (#f59e0b)."""
    html = _system_alert_html(
        alert_type="sweeper_consecutive_errors",
        severity="warning",
        title="Sweeper has failed 3 times in a row",
        detail={"consecutive_errors": 3},
    )
    assert "#f59e0b" in html
    assert "WARNING" in html


def test_system_alert_html_contains_alert_type():
    """The HTML includes the alert_type for identification."""
    html = _system_alert_html(
        alert_type="sweeper_stall",
        severity="critical",
        title="Sweeper stall",
        detail={"minutes_since_last_sweep": 20},
    )
    assert "sweeper_stall" in html


def test_system_alert_html_contains_title():
    """The HTML includes the title text."""
    title = "Error rate spike: 10 errors in 5m"
    html = _system_alert_html(
        alert_type="error_rate_spike",
        severity="critical",
        title=title,
        detail={},
    )
    assert title in html


def test_system_alert_html_renders_detail_keys():
    """Detail dict keys are rendered as human-readable labels."""
    html = _system_alert_html(
        alert_type="error_rate_spike",
        severity="critical",
        title="Test",
        detail={"error_count": 15, "sample_paths": ["/v1/a", "/v1/b"]},
    )
    assert "Error Count" in html
    assert "15" in html
    assert "Sample Paths" in html
    assert "/v1/a" in html
    assert "/v1/b" in html


def test_system_alert_html_list_values_joined():
    """List values in detail are comma-separated."""
    html = _system_alert_html(
        alert_type="test",
        severity="warning",
        title="Test",
        detail={"paths": ["/a", "/b", "/c"]},
    )
    assert "/a, /b, /c" in html


def test_system_alert_html_empty_list_shows_dash():
    """An empty list in detail renders as a dash."""
    html = _system_alert_html(
        alert_type="test",
        severity="warning",
        title="Test",
        detail={"items": []},
    )
    # The module uses "—" (em dash) for empty lists
    assert "\u2014" in html


def test_system_alert_html_contains_dashboard_link():
    """The HTML includes a link to the admin alert dashboard."""
    html = _system_alert_html(
        alert_type="test",
        severity="warning",
        title="Test",
        detail={},
    )
    assert "crowdsourcerer.rebaselabs.online/admin/alerts" in html


# ─── check_and_fire_alerts ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_and_fire_alerts_error_rate_spike():
    """Fires error_rate_spike when errors exceed threshold."""
    settings = system_alerts._settings
    threshold = settings.alert_error_rate_threshold

    # Fill the error window with enough errors to trigger the alert
    now = datetime.now(timezone.utc)
    for i in range(threshold):
        system_alerts._error_window.append((now, f"/v1/path_{i}"))

    mock_factory = AsyncMock()

    with patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire:
        await check_and_fire_alerts(mock_factory)

    mock_fire.assert_called_once()
    call_kwargs = mock_fire.call_args
    assert call_kwargs.kwargs["alert_type"] == "error_rate_spike"
    assert call_kwargs.kwargs["severity"] == "critical"
    assert "error_count" in call_kwargs.kwargs["detail"]


@pytest.mark.asyncio
async def test_check_and_fire_alerts_no_spike_below_threshold():
    """Does NOT fire error_rate_spike when errors are below threshold."""
    settings = system_alerts._settings
    threshold = settings.alert_error_rate_threshold

    now = datetime.now(timezone.utc)
    for i in range(threshold - 1):
        system_alerts._error_window.append((now, f"/v1/path_{i}"))

    mock_factory = AsyncMock()

    with patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire:
        await check_and_fire_alerts(mock_factory)

    # No alert should fire for error_rate_spike (sweeper checks may or may not fire)
    for call in mock_fire.call_args_list:
        assert call.kwargs.get("alert_type") != "error_rate_spike"


@pytest.mark.asyncio
async def test_check_and_fire_alerts_prunes_stale_entries():
    """Old errors outside the rolling window are pruned."""
    settings = system_alerts._settings
    window_minutes = settings.alert_error_rate_window_minutes

    # Add errors that are older than the window
    old_time = datetime.now(timezone.utc) - timedelta(minutes=window_minutes + 5)
    for i in range(20):
        system_alerts._error_window.append((old_time, f"/v1/old_{i}"))

    # Add one recent error (not enough to trigger)
    system_alerts._error_window.append((datetime.now(timezone.utc), "/v1/recent"))

    mock_factory = AsyncMock()

    with patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire:
        await check_and_fire_alerts(mock_factory)

    # Old entries should be pruned, only 1 recent remains
    assert len(system_alerts._error_window) == 1
    assert system_alerts._error_window[0][1] == "/v1/recent"

    # Should NOT fire error_rate_spike (only 1 error after pruning)
    for call in mock_fire.call_args_list:
        assert call.kwargs.get("alert_type") != "error_rate_spike"


@pytest.mark.asyncio
async def test_check_and_fire_alerts_respects_cooldown():
    """Does NOT re-fire an alert within the cooldown period."""
    settings = system_alerts._settings
    threshold = settings.alert_error_rate_threshold

    now = datetime.now(timezone.utc)
    for i in range(threshold):
        system_alerts._error_window.append((now, f"/v1/path_{i}"))

    # Simulate that we already fired this alert recently
    system_alerts._alert_cooldowns["error_rate_spike"] = now - timedelta(minutes=10)

    mock_factory = AsyncMock()

    with patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire:
        await check_and_fire_alerts(mock_factory)

    # error_rate_spike should NOT fire because it's still in cooldown
    for call in mock_fire.call_args_list:
        assert call.kwargs.get("alert_type") != "error_rate_spike"


@pytest.mark.asyncio
async def test_check_and_fire_alerts_fires_after_cooldown_expires():
    """Re-fires an alert after the cooldown period has elapsed."""
    settings = system_alerts._settings
    threshold = settings.alert_error_rate_threshold

    now = datetime.now(timezone.utc)
    for i in range(threshold):
        system_alerts._error_window.append((now, f"/v1/path_{i}"))

    # Cooldown expired (2 hours ago, default cooldown is 1 hour)
    system_alerts._alert_cooldowns["error_rate_spike"] = now - timedelta(hours=2)

    mock_factory = AsyncMock()

    with patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire:
        await check_and_fire_alerts(mock_factory)

    fired_types = [c.kwargs["alert_type"] for c in mock_fire.call_args_list]
    assert "error_rate_spike" in fired_types


@pytest.mark.asyncio
async def test_check_and_fire_alerts_sweeper_stall():
    """Fires sweeper_stall when the sweeper hasn't run within the threshold."""
    settings = system_alerts._settings
    stall_minutes = settings.alert_sweeper_stall_minutes

    # Set _LAST_SWEEP_AT to far enough in the past to trigger stall
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=stall_minutes + 5)

    mock_factory = AsyncMock()

    with (
        patch("core.sweeper._LAST_SWEEP_AT", stale_time),
        patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire,
    ):
        await check_and_fire_alerts(mock_factory)

    fired_types = [c.kwargs["alert_type"] for c in mock_fire.call_args_list]
    assert "sweeper_stall" in fired_types


@pytest.mark.asyncio
async def test_check_and_fire_alerts_sweeper_stall_not_fired_when_recent():
    """Does NOT fire sweeper_stall when the sweeper ran recently."""
    recent_time = datetime.now(timezone.utc) - timedelta(minutes=1)

    mock_factory = AsyncMock()

    with (
        patch("core.sweeper._LAST_SWEEP_AT", recent_time),
        patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire,
    ):
        await check_and_fire_alerts(mock_factory)

    fired_types = [c.kwargs["alert_type"] for c in mock_fire.call_args_list]
    assert "sweeper_stall" not in fired_types


@pytest.mark.asyncio
async def test_check_and_fire_alerts_sweeper_stall_skipped_when_none():
    """Does NOT fire sweeper_stall when _LAST_SWEEP_AT is None (never started)."""
    mock_factory = AsyncMock()

    with (
        patch("core.sweeper._LAST_SWEEP_AT", None),
        patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire,
    ):
        await check_and_fire_alerts(mock_factory)

    fired_types = [c.kwargs["alert_type"] for c in mock_fire.call_args_list]
    assert "sweeper_stall" not in fired_types


@pytest.mark.asyncio
async def test_check_and_fire_alerts_consecutive_sweep_errors():
    """Fires sweeper_consecutive_errors when 3+ consecutive errors."""
    system_alerts._sweep_consecutive_errors = 3

    mock_factory = AsyncMock()

    with (
        patch("core.sweeper._LAST_SWEEP_AT", None),
        patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire,
    ):
        await check_and_fire_alerts(mock_factory)

    fired_types = [c.kwargs["alert_type"] for c in mock_fire.call_args_list]
    assert "sweeper_consecutive_errors" in fired_types

    # Verify the severity is warning (not critical)
    for call in mock_fire.call_args_list:
        if call.kwargs["alert_type"] == "sweeper_consecutive_errors":
            assert call.kwargs["severity"] == "warning"
            assert call.kwargs["detail"]["consecutive_errors"] == 3


@pytest.mark.asyncio
async def test_check_and_fire_alerts_no_sweep_error_alert_below_3():
    """Does NOT fire sweeper_consecutive_errors when fewer than 3 errors."""
    system_alerts._sweep_consecutive_errors = 2

    mock_factory = AsyncMock()

    with (
        patch("core.sweeper._LAST_SWEEP_AT", None),
        patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire,
    ):
        await check_and_fire_alerts(mock_factory)

    fired_types = [c.kwargs["alert_type"] for c in mock_fire.call_args_list]
    assert "sweeper_consecutive_errors" not in fired_types


@pytest.mark.asyncio
async def test_check_and_fire_alerts_multiple_alerts_at_once():
    """Multiple alert types can fire in a single check cycle."""
    settings = system_alerts._settings
    threshold = settings.alert_error_rate_threshold
    stall_minutes = settings.alert_sweeper_stall_minutes

    # Trigger error_rate_spike
    now = datetime.now(timezone.utc)
    for i in range(threshold):
        system_alerts._error_window.append((now, f"/v1/path_{i}"))

    # Trigger sweeper_consecutive_errors
    system_alerts._sweep_consecutive_errors = 5

    # Trigger sweeper_stall
    stale_time = now - timedelta(minutes=stall_minutes + 10)

    mock_factory = AsyncMock()

    with (
        patch("core.sweeper._LAST_SWEEP_AT", stale_time),
        patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire,
    ):
        await check_and_fire_alerts(mock_factory)

    fired_types = {c.kwargs["alert_type"] for c in mock_fire.call_args_list}
    assert "error_rate_spike" in fired_types
    assert "sweeper_stall" in fired_types
    assert "sweeper_consecutive_errors" in fired_types


@pytest.mark.asyncio
async def test_check_and_fire_alerts_error_spike_includes_sample_paths():
    """The error_rate_spike alert includes up to 5 unique sample paths."""
    settings = system_alerts._settings
    threshold = settings.alert_error_rate_threshold

    now = datetime.now(timezone.utc)
    paths = ["/v1/a", "/v1/b", "/v1/c", "/v1/d", "/v1/e", "/v1/f", "/v1/g"]
    for i in range(threshold):
        system_alerts._error_window.append((now, paths[i % len(paths)]))

    mock_factory = AsyncMock()

    with patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire:
        await check_and_fire_alerts(mock_factory)

    for call in mock_fire.call_args_list:
        if call.kwargs["alert_type"] == "error_rate_spike":
            sample = call.kwargs["detail"]["sample_paths"]
            assert isinstance(sample, list)
            assert len(sample) <= 5


@pytest.mark.asyncio
async def test_check_and_fire_alerts_clean_state_no_alerts():
    """With fresh state and no errors, no alerts fire at all."""
    mock_factory = AsyncMock()

    with (
        patch("core.sweeper._LAST_SWEEP_AT", None),
        patch.object(system_alerts, "_fire_alert", new_callable=AsyncMock) as mock_fire,
    ):
        await check_and_fire_alerts(mock_factory)

    mock_fire.assert_not_called()
