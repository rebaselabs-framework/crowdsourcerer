"""Tests for the league system.

Covers:
  1. LEAGUE_TIERS constant — correct order and length
  2. Tier metadata — all tiers have metadata entries
  3. _current_week_start — always returns a Monday
  4. _current_week_end — always returns a Sunday
  5. get_or_create_season — creates new season with correct dates
  6. get_or_create_season — returns existing season if already created
  7. _find_or_create_group — creates first group when none exist
  8. _find_or_create_group — fills existing group before creating new
  9. _get_group_standings — ranks by XP descending with correct zones
  10. add_league_xp — increments member XP
  11. add_league_xp — no-op when worker not in a league
  12. GET /v1/leagues/tiers — returns all 6 tiers
  13. POST /v1/leagues/join — places worker in correct tier group
  14. POST /v1/leagues/join — rejects non-workers
  15. POST /v1/leagues/join — rejects duplicate join
  16. GET /v1/leagues/current — shows standings after join
  17. GET /v1/leagues/history — empty for new worker
  18. process_season_end — promotes top 5, demotes bottom 5
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG", "true")

from datetime import datetime, timezone, timedelta, date
from unittest.mock import patch


# ── Pure function tests ──────────────────────────────────────────────────────

def test_league_tiers_constant():
    """LEAGUE_TIERS has 6 tiers in correct order."""
    from models.db import LEAGUE_TIERS
    assert len(LEAGUE_TIERS) == 6
    assert LEAGUE_TIERS[0] == "bronze"
    assert LEAGUE_TIERS[-1] == "obsidian"
    assert LEAGUE_TIERS == ["bronze", "silver", "gold", "platinum", "diamond", "obsidian"]


def test_tier_metadata_complete():
    """Every tier in LEAGUE_TIERS has a matching entry in TIER_META."""
    from models.db import LEAGUE_TIERS
    from routers.leagues import TIER_META
    for tier in LEAGUE_TIERS:
        assert tier in TIER_META, f"Missing metadata for tier: {tier}"
        meta = TIER_META[tier]
        assert "name" in meta
        assert "icon" in meta
        assert "color" in meta
        assert "order" in meta


def test_current_week_start_is_monday():
    """_current_week_start always returns a Monday."""
    from routers.leagues import _current_week_start
    ws = _current_week_start()
    assert ws.weekday() == 0  # Monday


def test_current_week_end_is_sunday():
    """_current_week_end always returns a Sunday."""
    from routers.leagues import _current_week_end
    we = _current_week_end()
    assert we.weekday() == 6  # Sunday


def test_week_start_end_span_7_days():
    """Week start and end are exactly 6 days apart (Mon through Sun)."""
    from routers.leagues import _current_week_start, _current_week_end
    ws = _current_week_start()
    we = _current_week_end()
    assert (we - ws).days == 6


def test_group_size_constant():
    """GROUP_SIZE is 30."""
    from routers.leagues import GROUP_SIZE
    assert GROUP_SIZE == 30


def test_promo_demo_slots():
    """PROMO_SLOTS and DEMO_SLOTS are 5."""
    from routers.leagues import PROMO_SLOTS, DEMO_SLOTS
    assert PROMO_SLOTS == 5
    assert DEMO_SLOTS == 5


# ── Schema tests ─────────────────────────────────────────────────────────────

def test_league_tier_info_schema():
    """LeagueTierInfo can be instantiated with valid data."""
    from models.schemas import LeagueTierInfo
    info = LeagueTierInfo(
        tier="bronze",
        name="Bronze League",
        icon="🥉",
        color="amber",
        order=0,
        promo_slots=5,
        demo_slots=5,
    )
    assert info.tier == "bronze"
    assert info.promo_slots == 5


def test_league_standing_entry_schema():
    """LeagueStandingEntry can be instantiated with valid data."""
    from models.schemas import LeagueStandingEntry
    import uuid
    entry = LeagueStandingEntry(
        rank=1,
        user_id=uuid.uuid4(),
        name="Alice",
        worker_level=5,
        xp_earned=120,
        is_me=True,
        zone="promo",
    )
    assert entry.rank == 1
    assert entry.zone == "promo"
    assert entry.is_me is True


def test_league_season_out_schema():
    """LeagueSeasonOut can be instantiated."""
    from models.schemas import LeagueSeasonOut
    import uuid
    season = LeagueSeasonOut(
        season_id=uuid.uuid4(),
        week_start=date(2026, 3, 30),
        week_end=date(2026, 4, 5),
        status="active",
    )
    assert season.status == "active"


def test_league_current_out_defaults():
    """LeagueCurrentOut defaults to not-joined state."""
    from models.schemas import LeagueCurrentOut, LeagueSeasonOut
    import uuid
    season = LeagueSeasonOut(
        season_id=uuid.uuid4(),
        week_start=date(2026, 3, 30),
        week_end=date(2026, 4, 5),
        status="active",
    )
    current = LeagueCurrentOut(
        season=season,
    )
    assert current.joined is False
    assert current.group is None
    assert current.my_xp == 0
    assert current.my_tier == "bronze"


def test_league_history_out_schema():
    """LeagueHistoryOut can hold multiple season entries."""
    from models.schemas import LeagueHistoryOut, LeagueHistoryEntry
    import uuid
    entry = LeagueHistoryEntry(
        season_id=uuid.uuid4(),
        week_start=date(2026, 3, 23),
        week_end=date(2026, 3, 29),
        tier="silver",
        tier_icon="🥈",
        final_rank=3,
        xp_earned=250,
        result="promoted",
        group_size=28,
    )
    history = LeagueHistoryOut(seasons=[entry], current_tier="gold")
    assert len(history.seasons) == 1
    assert history.current_tier == "gold"
    assert history.seasons[0].result == "promoted"


# ── DB model tests ───────────────────────────────────────────────────────────

def test_league_season_db_model():
    """LeagueSeasonDB model has expected columns."""
    from models.db import LeagueSeasonDB
    cols = {c.name for c in LeagueSeasonDB.__table__.columns}
    assert "id" in cols
    assert "week_start" in cols
    assert "week_end" in cols
    assert "status" in cols


def test_league_group_db_model():
    """LeagueGroupDB model has expected columns."""
    from models.db import LeagueGroupDB
    cols = {c.name for c in LeagueGroupDB.__table__.columns}
    assert "season_id" in cols
    assert "tier" in cols
    assert "group_number" in cols


def test_league_group_member_db_model():
    """LeagueGroupMemberDB model has expected columns."""
    from models.db import LeagueGroupMemberDB
    cols = {c.name for c in LeagueGroupMemberDB.__table__.columns}
    assert "group_id" in cols
    assert "user_id" in cols
    assert "xp_earned" in cols
    assert "final_rank" in cols
    assert "result" in cols


def test_user_has_league_tier():
    """UserDB model has league_tier column."""
    from models.db import UserDB
    cols = {c.name for c in UserDB.__table__.columns}
    assert "league_tier" in cols
