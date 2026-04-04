"""Tests for the quest system.

Covers:
  1. QUEST_TEMPLATES — all templates have required fields
  2. QUEST_TEMPLATES — difficulty distribution (easy, medium, hard)
  3. QUEST_TEMPLATES — quest types are valid
  4. _current_week_start — always Monday
  5. _current_week_end — always Sunday
  6. Schema: QuestOut can be instantiated
  7. Schema: QuestProgressOut can be instantiated
  8. Schema: WeeklyQuestsOut can be instantiated
  9. DB model: ActiveQuestDB has expected columns
  10. DB model: QuestProgressDB has expected columns
  11. DB model: QuestProgressDB has unique constraint
  12. Quest templates have positive rewards
  13. Quest templates have positive targets
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET", "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG", "true")

from datetime import date
import uuid


# ── Template tests ───────────────────────────────────────────────────────────

def test_quest_templates_have_required_fields():
    """All quest templates have the required fields."""
    from routers.quests import QUEST_TEMPLATES
    required = {"quest_key", "title", "description", "icon", "quest_type",
                "target_value", "xp_reward", "credits_reward", "difficulty"}
    for tmpl in QUEST_TEMPLATES:
        missing = required - set(tmpl.keys())
        assert not missing, f"Template {tmpl.get('quest_key', '?')} missing: {missing}"


def test_quest_templates_difficulty_distribution():
    """Templates have at least 1 easy, 2 medium, and 1 hard."""
    from routers.quests import QUEST_TEMPLATES
    difficulties = [t["difficulty"] for t in QUEST_TEMPLATES]
    assert difficulties.count("easy") >= 1
    assert difficulties.count("medium") >= 2
    assert difficulties.count("hard") >= 1


def test_quest_templates_valid_types():
    """All quest types are in the valid set."""
    from routers.quests import QUEST_TEMPLATES
    valid_types = {"volume", "streak", "variety", "accuracy", "challenge"}
    for tmpl in QUEST_TEMPLATES:
        assert tmpl["quest_type"] in valid_types, f"Invalid type: {tmpl['quest_type']}"


def test_quest_templates_positive_rewards():
    """All templates have positive XP and credit rewards."""
    from routers.quests import QUEST_TEMPLATES
    for tmpl in QUEST_TEMPLATES:
        assert tmpl["xp_reward"] > 0, f"{tmpl['quest_key']}: xp_reward must be positive"
        assert tmpl["credits_reward"] > 0, f"{tmpl['quest_key']}: credits_reward must be positive"


def test_quest_templates_positive_targets():
    """All templates have positive target values."""
    from routers.quests import QUEST_TEMPLATES
    for tmpl in QUEST_TEMPLATES:
        assert tmpl["target_value"] > 0, f"{tmpl['quest_key']}: target_value must be positive"


def test_quest_templates_unique_keys():
    """All quest_key values are unique."""
    from routers.quests import QUEST_TEMPLATES
    keys = [t["quest_key"] for t in QUEST_TEMPLATES]
    assert len(keys) == len(set(keys)), "Duplicate quest_key found"


def test_quest_templates_valid_difficulty():
    """All difficulties are easy, medium, or hard."""
    from routers.quests import QUEST_TEMPLATES
    valid = {"easy", "medium", "hard"}
    for tmpl in QUEST_TEMPLATES:
        assert tmpl["difficulty"] in valid


# ── Pure function tests ──────────────────────────────────────────────────────

def test_current_week_start_is_monday():
    """_current_week_start always returns a Monday."""
    from routers.quests import _current_week_start
    ws = _current_week_start()
    assert ws.weekday() == 0


def test_current_week_end_is_sunday():
    """_current_week_end always returns a Sunday."""
    from routers.quests import _current_week_end
    we = _current_week_end()
    assert we.weekday() == 6


# ── Schema tests ─────────────────────────────────────────────────────────────

def test_quest_out_schema():
    """QuestOut can be instantiated."""
    from models.schemas import QuestOut
    q = QuestOut(
        id=uuid.uuid4(),
        quest_key="volume_5",
        title="Getting Started",
        description="Complete 5 tasks",
        icon="📋",
        quest_type="volume",
        target_value=5,
        xp_reward=50,
        credits_reward=10,
        difficulty="easy",
    )
    assert q.quest_type == "volume"
    assert q.target_value == 5


def test_quest_progress_out_schema():
    """QuestProgressOut can be instantiated."""
    from models.schemas import QuestOut, QuestProgressOut
    quest = QuestOut(
        id=uuid.uuid4(),
        quest_key="volume_5",
        title="Getting Started",
        description="Complete 5 tasks",
        icon="📋",
        quest_type="volume",
        target_value=5,
        xp_reward=50,
        credits_reward=10,
        difficulty="easy",
    )
    progress = QuestProgressOut(
        quest=quest,
        current_value=3,
        target_value=5,
        is_complete=False,
        is_claimed=False,
        progress_pct=60.0,
    )
    assert progress.current_value == 3
    assert progress.progress_pct == 60.0


def test_weekly_quests_out_schema():
    """WeeklyQuestsOut can hold quest progress entries."""
    from models.schemas import WeeklyQuestsOut
    wq = WeeklyQuestsOut(
        quests=[],
        week_start=date(2026, 3, 30),
        week_end=date(2026, 4, 5),
        total_completed=0,
        total_claimed=0,
    )
    assert wq.total_completed == 0
    assert len(wq.quests) == 0


# ── DB model tests ───────────────────────────────────────────────────────────

def test_active_quest_db_model():
    """ActiveQuestDB model has expected columns."""
    from models.db import ActiveQuestDB
    cols = {c.name for c in ActiveQuestDB.__table__.columns}
    expected = {"id", "quest_key", "title", "description", "icon", "quest_type",
                "target_value", "xp_reward", "credits_reward", "difficulty",
                "week_start", "week_end", "created_at"}
    assert expected.issubset(cols)


def test_quest_progress_db_model():
    """QuestProgressDB model has expected columns."""
    from models.db import QuestProgressDB
    cols = {c.name for c in QuestProgressDB.__table__.columns}
    expected = {"id", "quest_id", "user_id", "current_value", "is_complete",
                "completed_at", "is_claimed", "claimed_at", "extra_data", "created_at"}
    assert expected.issubset(cols)


def test_quest_progress_unique_constraint():
    """QuestProgressDB has a unique constraint on (quest_id, user_id)."""
    from models.db import QuestProgressDB
    constraints = [c.name for c in QuestProgressDB.__table__.constraints
                   if hasattr(c, 'columns') and len(getattr(c, 'columns', [])) > 1]
    # Check that the unique constraint exists
    table_args = getattr(QuestProgressDB, '__table_args__', ())
    from sqlalchemy import UniqueConstraint
    has_uq = any(
        isinstance(arg, UniqueConstraint)
        for arg in (table_args if isinstance(table_args, tuple) else [table_args])
    )
    assert has_uq, "QuestProgressDB should have a UniqueConstraint"
