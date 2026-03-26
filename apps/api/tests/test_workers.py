"""Unit tests for the task router (no RebaseKit calls needed)."""
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/crowdsourcerer_test")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("API_KEY_SALT", "test-salt")

import pytest
from unittest.mock import AsyncMock, MagicMock

from workers.router import execute_task, TASK_CREDITS
from workers.base import WorkerError


def make_mock_client(**kwargs):
    client = MagicMock()
    client.post = AsyncMock(**kwargs)
    return client


@pytest.mark.asyncio
async def test_task_credits_coverage():
    """All task types should have credit costs defined."""
    task_types = [
        "web_research", "entity_lookup", "document_parse", "data_transform",
        "llm_generate", "screenshot", "audio_transcribe", "pii_detect",
        "code_execute", "web_intel",
    ]
    for t in task_types:
        assert t in TASK_CREDITS, f"Missing credits for {t}"
        assert TASK_CREDITS[t] > 0


@pytest.mark.asyncio
async def test_unknown_task_raises():
    client = make_mock_client()
    with pytest.raises(WorkerError) as exc_info:
        await execute_task("definitely_not_a_type", {}, client)
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_llm_generate_routing():
    client = make_mock_client(return_value={
        "choices": [{"message": {"role": "assistant", "content": "Hello!"}}]
    })
    result = await execute_task("llm_generate", {
        "messages": [{"role": "user", "content": "Say hello"}]
    }, client)
    client.post.assert_called_once()
    assert result["summary"] == "Hello!"


@pytest.mark.asyncio
async def test_pii_detect_routing():
    client = make_mock_client(return_value={"entities": [
        {"type": "EMAIL", "value": "test@example.com"}
    ]})
    result = await execute_task("pii_detect", {"text": "My email is test@example.com"}, client)
    assert "1 PII" in result["summary"]


@pytest.mark.asyncio
async def test_web_research_routing():
    client = make_mock_client(return_value={
        "summary": "Page content here",
        "content": "Full page content",
    })
    result = await execute_task("web_research", {"url": "https://example.com"}, client)
    assert result["summary"] == "Page content here"


# ── compute_level() ───────────────────────────────────────────────────────────

def level_of(xp):
    from routers.worker import compute_level
    return compute_level(xp)


def test_level_1_at_zero_xp():
    """A brand-new worker starts at level 1 with 100 XP needed to advance."""
    level, xp_to_next = level_of(0)
    assert level == 1
    assert xp_to_next == 100


def test_level_1_below_threshold():
    """Workers below 100 XP stay at level 1."""
    level, xp_to_next = level_of(99)
    assert level == 1
    assert xp_to_next == 1


def test_level_2_at_exact_threshold():
    """At exactly 100 XP the worker advances to level 2."""
    level, xp_to_next = level_of(100)
    assert level == 2


def test_level_2_xp_to_next():
    """Level 2 requires 250 XP total; at 100 XP, 150 remain."""
    _, xp_to_next = level_of(100)
    assert xp_to_next == 150


def test_level_3_threshold():
    """250 XP → level 3."""
    level, _ = level_of(250)
    assert level == 3


def test_level_5_threshold():
    """1000 XP → level 5."""
    level, _ = level_of(1000)
    assert level == 5


def test_level_10_threshold():
    """11000 XP → level 10."""
    level, _ = level_of(11000)
    assert level == 10


def test_max_level_20():
    """96000 XP → max level 20, 0 XP to next."""
    level, xp_to_next = level_of(96000)
    assert level == 20
    assert xp_to_next == 0


def test_beyond_max_level_capped():
    """More XP than max threshold is still level 20."""
    level, xp_to_next = level_of(999_999)
    assert level == 20
    assert xp_to_next == 0


def test_xp_to_next_never_negative():
    """xp_to_next must never be negative even with unusual XP values."""
    for xp in [0, 1, 50, 100, 500, 15000, 96000, 200000]:
        _, xp_to_next = level_of(xp)
        assert xp_to_next >= 0, f"xp_to_next negative for xp={xp}"


def test_level_names_table_coverage():
    """LEVEL_NAMES must have an entry for every level 1..20."""
    from routers.worker import LEVEL_NAMES, LEVEL_THRESHOLDS
    # LEVEL_NAMES[0] is empty string (padding), [1] is "Apprentice", etc.
    assert len(LEVEL_NAMES) == len(LEVEL_THRESHOLDS) + 1, (
        "LEVEL_NAMES must have len(LEVEL_THRESHOLDS)+1 entries (index 0 is padding)"
    )
    for i in range(1, len(LEVEL_THRESHOLDS) + 1):
        assert LEVEL_NAMES[i], f"LEVEL_NAMES[{i}] is empty"


def test_xp_base_covers_all_human_task_types():
    """Every human task type must have an XP reward defined."""
    from routers.worker import TASK_XP_BASE
    human_types = [
        "label_image", "label_text", "rate_quality", "verify_fact",
        "moderate_content", "compare_rank", "answer_question", "transcription_review",
    ]
    for t in human_types:
        assert t in TASK_XP_BASE, f"Missing XP reward for task type: {t}"
        assert TASK_XP_BASE[t] > 0, f"XP reward for {t} must be positive"


# ── streak_xp_multiplier() ────────────────────────────────────────────────────

def multiplier(streak):
    from routers.worker import streak_xp_multiplier
    return streak_xp_multiplier(streak)


def test_streak_multiplier_zero_days():
    """New worker with 0 days gets 1× (no bonus)."""
    assert multiplier(0) == 1.0


def test_streak_multiplier_1_day():
    """1-day streak is below the first bonus tier (3 days) → 1×."""
    assert multiplier(1) == 1.0


def test_streak_multiplier_2_days():
    """2-day streak is below the first bonus tier → 1×."""
    assert multiplier(2) == 1.0


def test_streak_multiplier_3_days():
    """3-day streak hits the first bonus tier → 1.1×."""
    assert multiplier(3) == 1.1


def test_streak_multiplier_6_days():
    """6-day streak (below 7) → 1.1×."""
    assert multiplier(6) == 1.1


def test_streak_multiplier_7_days():
    """7-day streak → 1.25×."""
    assert multiplier(7) == 1.25


def test_streak_multiplier_13_days():
    """13-day streak (below 14) → 1.25×."""
    assert multiplier(13) == 1.25


def test_streak_multiplier_14_days():
    """14-day streak → 1.5×."""
    assert multiplier(14) == 1.5


def test_streak_multiplier_29_days():
    """29-day streak (below 30) → 1.5×."""
    assert multiplier(29) == 1.5


def test_streak_multiplier_30_days():
    """30-day streak → 2× (max bonus)."""
    assert multiplier(30) == 2.0


def test_streak_multiplier_100_days():
    """100-day streak still caps at 2×."""
    assert multiplier(100) == 2.0


def test_streak_multiplier_tiers_are_monotone():
    """Each higher streak tier must have a multiplier >= lower tier."""
    from routers.worker import STREAK_MULTIPLIER_TIERS
    # Tiers are sorted descending by threshold: (30, 2.0), (14, 1.5), ...
    multipliers = [m for _, m in STREAK_MULTIPLIER_TIERS]
    for i in range(len(multipliers) - 1):
        assert multipliers[i] >= multipliers[i + 1], (
            f"Tier {i} multiplier {multipliers[i]} < tier {i+1} multiplier {multipliers[i+1]}"
        )


# ── compute_xp_for_task with streak ──────────────────────────────────────────

def xp(task_type, accurate=True, streak_days=0):
    from routers.worker import compute_xp_for_task
    return compute_xp_for_task(task_type, accurate=accurate, streak_days=streak_days)


def test_compute_xp_no_streak():
    """Without a streak, XP equals the base value."""
    from routers.worker import TASK_XP_BASE
    for t, base in TASK_XP_BASE.items():
        assert xp(t, streak_days=0) == base


def test_compute_xp_7_day_streak():
    """7-day streak applies 1.25× multiplier."""
    from routers.worker import TASK_XP_BASE
    base = TASK_XP_BASE["verify_fact"]  # 12 XP base
    assert xp("verify_fact", streak_days=7) == round(base * 1.25)


def test_compute_xp_30_day_streak():
    """30-day streak applies 2× multiplier."""
    from routers.worker import TASK_XP_BASE
    base = TASK_XP_BASE["answer_question"]  # 15 XP base
    assert xp("answer_question", streak_days=30) == round(base * 2.0)


def test_compute_xp_inaccurate_halved():
    """Inaccurate submission halves base XP before streak multiplier."""
    from routers.worker import TASK_XP_BASE
    base = TASK_XP_BASE["label_image"]  # 10 XP
    # Inaccurate: base//2 = 5, then no streak = 5
    assert xp("label_image", accurate=False, streak_days=0) == 5


def test_compute_xp_inaccurate_with_streak():
    """Inaccurate submissions still get a streak multiplier."""
    from routers.worker import TASK_XP_BASE
    base = TASK_XP_BASE["label_image"]  # 10 XP → inaccurate = 5
    # 7-day streak: 5 * 1.25 = 6.25 → round = 6
    result = xp("label_image", accurate=False, streak_days=7)
    assert result == round(5 * 1.25)


def test_compute_xp_always_positive():
    """XP is always at least 1 even for inaccurate submissions."""
    # The minimum 1 comes from max(1, base//2) before multiplier
    result = xp("label_text", accurate=False, streak_days=0)
    assert result >= 1
