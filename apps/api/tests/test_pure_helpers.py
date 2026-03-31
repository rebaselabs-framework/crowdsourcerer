"""Unit tests for pure helper functions and data constants across several modules.

routers/skills.py — _proficiency_for_completed:
  1.  0 tasks → proficiency 1
  2.  1 task  → proficiency 1 (below volume threshold for 2)
  3.  5 tasks → proficiency 2 (first threshold)
  4.  4 tasks → proficiency 1 (just below threshold for 2)
  5.  20 tasks → proficiency 3
  6.  19 tasks → proficiency 2 (just below threshold for 3)
  7.  50 tasks → proficiency 4
  8.  49 tasks → proficiency 3 (just below threshold for 4)
  9.  100 tasks → proficiency 5
  10. 99 tasks → proficiency 4 (just below threshold for 5)
  11. accuracy >= 0.95 bumps base up by 1
  12. accuracy < 0.70 bumps base down by 1
  13. accuracy=None → no modification to base
  14. accuracy bump up capped at 5 (not 6)
  15. accuracy bump down floored at 1 (not 0)
  16. accuracy in 0.70–0.95 range → no modification
  17. result always in [1, 5]

routers/skills.py — HUMAN_TASK_TYPES / AI_TASK_TYPES data integrity:
  18. HUMAN_TASK_TYPES is non-empty
  19. AI_TASK_TYPES is non-empty
  20. HUMAN_TASK_TYPES and AI_TASK_TYPES are disjoint (no overlap)

models/schemas.py — PROFICIENCY_LABELS:
  21. Has exactly 5 entries (keys 1–5)
  22. All keys are integers 1–5
  23. All values are non-empty strings
  24. Labels are distinct

core/result_cache.py — _input_hash:
  25. Returns a 64-char hex string (SHA-256)
  26. Same task_type + input → same hash
  27. Different task_type → different hash
  28. Different input value → different hash
  29. Input key order does not matter (sort_keys=True)
  30. Nested dict keys also sorted (recursive)

core/result_cache.py — _DEFAULT_TTL_HOURS:
  31. All TTL values are non-negative integers
  32. time-sensitive types (web_research, screenshot) have short TTLs > 0
  33. deterministic types (document_parse, pii_detect) have TTL = 0

core/webhooks.py — _render_payload_template:
  34. {{key}} replaced with context value
  35. {{nested.key}} uses dot notation into sub-dict
  36. Missing top-level key → empty string
  37. Missing nested key → empty string
  38. Dict value → JSON string
  39. List value → JSON string
  40. No placeholders → identity JSON parse
  41. Invalid JSON after rendering → {"_raw": rendered}
  42. None value → empty string

core/webhooks.py — ALL_EVENTS / DEFAULT_EVENTS:
  43. ALL_EVENTS has no duplicates
  44. All event strings follow "resource.action" pattern
  45. DEFAULT_EVENTS is a subset of ALL_EVENTS
"""
from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")


# ── _proficiency_for_completed ────────────────────────────────────────────────

def test_proficiency_0_tasks_is_1():
    from routers.skills import _proficiency_for_completed
    assert _proficiency_for_completed(0, None) == 1


def test_proficiency_1_task_is_1():
    from routers.skills import _proficiency_for_completed
    assert _proficiency_for_completed(1, None) == 1


def test_proficiency_4_tasks_is_1():
    """Just below the threshold for level 2."""
    from routers.skills import _proficiency_for_completed
    assert _proficiency_for_completed(4, None) == 1


def test_proficiency_5_tasks_is_2():
    """First volume threshold: tasks>=5 → base 2."""
    from routers.skills import _proficiency_for_completed
    assert _proficiency_for_completed(5, None) == 2


def test_proficiency_19_tasks_is_2():
    """Just below the threshold for level 3."""
    from routers.skills import _proficiency_for_completed
    assert _proficiency_for_completed(19, None) == 2


def test_proficiency_20_tasks_is_3():
    """tasks>=20 → base 3."""
    from routers.skills import _proficiency_for_completed
    assert _proficiency_for_completed(20, None) == 3


def test_proficiency_49_tasks_is_3():
    """Just below the threshold for level 4."""
    from routers.skills import _proficiency_for_completed
    assert _proficiency_for_completed(49, None) == 3


def test_proficiency_50_tasks_is_4():
    """tasks>=50 → base 4."""
    from routers.skills import _proficiency_for_completed
    assert _proficiency_for_completed(50, None) == 4


def test_proficiency_99_tasks_is_4():
    """Just below the threshold for level 5."""
    from routers.skills import _proficiency_for_completed
    assert _proficiency_for_completed(99, None) == 4


def test_proficiency_100_tasks_is_5():
    """tasks>=100 → base 5."""
    from routers.skills import _proficiency_for_completed
    assert _proficiency_for_completed(100, None) == 5


def test_proficiency_high_accuracy_bumps_up():
    """accuracy >= 0.95 bumps base level up by 1."""
    from routers.skills import _proficiency_for_completed
    # base = 2 (5 tasks), accuracy=0.95 → 3
    assert _proficiency_for_completed(5, 0.95) == 3


def test_proficiency_low_accuracy_bumps_down():
    """accuracy < 0.70 bumps base level down by 1."""
    from routers.skills import _proficiency_for_completed
    # base = 3 (20 tasks), accuracy=0.65 → 2
    assert _proficiency_for_completed(20, 0.65) == 2


def test_proficiency_accuracy_none_no_change():
    """accuracy=None → base level unchanged."""
    from routers.skills import _proficiency_for_completed
    base_no_acc = _proficiency_for_completed(20, None)   # = 3
    base_mid_acc = _proficiency_for_completed(20, 0.80)  # 0.70 <= 0.80 < 0.95 → no change
    assert base_no_acc == 3
    assert base_mid_acc == 3


def test_proficiency_accuracy_bump_up_capped_at_5():
    """Bump-up at base=5 cannot exceed 5."""
    from routers.skills import _proficiency_for_completed
    # base = 5 (100 tasks), accuracy=0.99 → min(5, 5+1) = 5
    assert _proficiency_for_completed(100, 0.99) == 5


def test_proficiency_accuracy_bump_down_floored_at_1():
    """Bump-down at base=1 cannot go below 1."""
    from routers.skills import _proficiency_for_completed
    # base = 1 (1 task), accuracy=0.50 → max(1, 1-1) = 1
    assert _proficiency_for_completed(1, 0.50) == 1


def test_proficiency_mid_accuracy_no_change():
    """Accuracy in [0.70, 0.95) causes no modification."""
    from routers.skills import _proficiency_for_completed
    assert _proficiency_for_completed(20, 0.70) == 3   # exact lower bound — no change
    assert _proficiency_for_completed(20, 0.80) == 3
    assert _proficiency_for_completed(20, 0.94) == 3


def test_proficiency_always_in_range():
    """Return value is always in [1, 5]."""
    from routers.skills import _proficiency_for_completed
    combos = [
        (0, None), (1, 0.0), (5, 0.5), (20, 0.99),
        (50, 0.99), (100, 0.0), (200, 0.99),
    ]
    for tasks, acc in combos:
        result = _proficiency_for_completed(tasks, acc)
        assert 1 <= result <= 5, f"Out of range {result} for ({tasks}, {acc})"


# ── HUMAN_TASK_TYPES / AI_TASK_TYPES data integrity ───────────────────────────

def test_human_task_types_non_empty():
    from routers.skills import HUMAN_TASK_TYPES
    assert len(HUMAN_TASK_TYPES) > 0


def test_ai_task_types_non_empty():
    from routers.skills import AI_TASK_TYPES
    assert len(AI_TASK_TYPES) > 0


def test_human_and_ai_task_types_disjoint():
    """No task type can be both human and AI."""
    from routers.skills import HUMAN_TASK_TYPES, AI_TASK_TYPES
    overlap = HUMAN_TASK_TYPES & AI_TASK_TYPES
    assert not overlap, f"Task types in both sets: {overlap}"


# ── PROFICIENCY_LABELS integrity ──────────────────────────────────────────────

def test_proficiency_labels_has_five_levels():
    from models.schemas import PROFICIENCY_LABELS
    assert len(PROFICIENCY_LABELS) == 5


def test_proficiency_labels_keys_are_1_through_5():
    from models.schemas import PROFICIENCY_LABELS
    assert set(PROFICIENCY_LABELS.keys()) == {1, 2, 3, 4, 5}


def test_proficiency_labels_values_non_empty_strings():
    from models.schemas import PROFICIENCY_LABELS
    for level, label in PROFICIENCY_LABELS.items():
        assert isinstance(label, str) and len(label) > 0, f"Empty label for level {level}"


def test_proficiency_labels_are_distinct():
    from models.schemas import PROFICIENCY_LABELS
    labels = list(PROFICIENCY_LABELS.values())
    assert len(labels) == len(set(labels)), "Duplicate label in PROFICIENCY_LABELS"


# ── _input_hash ───────────────────────────────────────────────────────────────

def test_input_hash_returns_64_char_hex():
    from core.result_cache import _input_hash
    h = _input_hash("label_text", {"text": "hello"})
    assert isinstance(h, str)
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_input_hash_same_inputs_produce_same_hash():
    from core.result_cache import _input_hash
    h1 = _input_hash("label_text", {"text": "hello"})
    h2 = _input_hash("label_text", {"text": "hello"})
    assert h1 == h2


def test_input_hash_different_task_type_different_hash():
    from core.result_cache import _input_hash
    h1 = _input_hash("label_text", {"text": "hello"})
    h2 = _input_hash("label_image", {"text": "hello"})
    assert h1 != h2


def test_input_hash_different_input_different_hash():
    from core.result_cache import _input_hash
    h1 = _input_hash("label_text", {"text": "hello"})
    h2 = _input_hash("label_text", {"text": "world"})
    assert h1 != h2


def test_input_hash_key_order_independent():
    """Dict key order does not affect the hash."""
    from core.result_cache import _input_hash
    h1 = _input_hash("data_transform", {"b": 2, "a": 1})
    h2 = _input_hash("data_transform", {"a": 1, "b": 2})
    assert h1 == h2


def test_input_hash_nested_key_order_independent():
    """Nested dicts also get their keys sorted."""
    from core.result_cache import _input_hash
    h1 = _input_hash("data_transform", {"outer": {"z": 3, "a": 1}})
    h2 = _input_hash("data_transform", {"outer": {"a": 1, "z": 3}})
    assert h1 == h2


# ── _DEFAULT_TTL_HOURS data integrity ─────────────────────────────────────────

def test_default_ttl_hours_all_non_negative():
    from core.result_cache import _DEFAULT_TTL_HOURS
    for task_type, ttl in _DEFAULT_TTL_HOURS.items():
        assert ttl >= 0, f"Negative TTL for '{task_type}': {ttl}"


def test_default_ttl_hours_web_research_short():
    """web_research TTL is short but non-zero (live content)."""
    from core.result_cache import _DEFAULT_TTL_HOURS
    ttl = _DEFAULT_TTL_HOURS.get("web_research", -1)
    assert 0 < ttl <= 6, f"Expected short TTL for web_research, got {ttl}"


def test_default_ttl_hours_deterministic_types_zero():
    """Deterministic types (document_parse, pii_detect) have TTL=0."""
    from core.result_cache import _DEFAULT_TTL_HOURS
    for task_type in ("document_parse", "pii_detect", "code_execute"):
        ttl = _DEFAULT_TTL_HOURS.get(task_type, -1)
        assert ttl == 0, f"Expected TTL=0 for deterministic type '{task_type}', got {ttl}"


# ── _render_payload_template ──────────────────────────────────────────────────

def test_render_simple_placeholder():
    """{{key}} is replaced by the corresponding context value."""
    from core.webhooks import _render_payload_template
    result = _render_payload_template('{"event": "{{name}}"}', {"name": "task.completed"})
    assert result == {"event": "task.completed"}


def test_render_dot_notation():
    """{{data.field}} traverses nested dict."""
    from core.webhooks import _render_payload_template
    ctx = {"data": {"plan": "pro"}}
    result = _render_payload_template('{"plan": "{{data.plan}}"}', ctx)
    assert result == {"plan": "pro"}


def test_render_missing_top_level_key_gives_empty_string():
    """Missing top-level key → empty string substituted."""
    from core.webhooks import _render_payload_template
    result = _render_payload_template('{"val": "{{missing}}"}', {})
    assert result == {"val": ""}


def test_render_missing_nested_key_gives_empty_string():
    """Missing nested key traversal → empty string."""
    from core.webhooks import _render_payload_template
    ctx = {"data": {}}
    result = _render_payload_template('{"val": "{{data.missing}}"}', ctx)
    assert result == {"val": ""}


def test_render_dict_value_unquoted_embedding():
    """Dict value embedded WITHOUT surrounding quotes is valid JSON and gets parsed."""
    from core.webhooks import _render_payload_template
    ctx = {"meta": {"a": 1}}
    # Direct (unquoted) embedding: the dict JSON is inserted directly as a value
    result = _render_payload_template('{"info": {{meta}}}', ctx)
    assert result == {"info": {"a": 1}}


def test_render_dict_value_quoted_produces_error():
    """Dict value inside quotes creates invalid JSON → returns error indicator (no internal data leak)."""
    from core.webhooks import _render_payload_template
    ctx = {"meta": {"a": 1}}
    # The inner JSON string breaks the outer JSON structure
    result = _render_payload_template('{"info": "{{meta}}"}', ctx)
    assert result["error"] == "template_render_failed"
    # Must NOT contain the rendered template content (prevents internal data leakage)
    assert "_raw" not in result


def test_render_list_value_unquoted_embedding():
    """List value embedded WITHOUT surrounding quotes is valid JSON."""
    from core.webhooks import _render_payload_template
    ctx = {"tags": ["x", "y"]}
    result = _render_payload_template('{"tags": {{tags}}}', ctx)
    assert result == {"tags": ["x", "y"]}


def test_render_no_placeholders_identity_parse():
    """Template with no placeholders parses as-is."""
    from core.webhooks import _render_payload_template
    result = _render_payload_template('{"static": "value"}', {})
    assert result == {"static": "value"}


def test_render_invalid_json_after_substitution_returns_error():
    """If rendered string is not valid JSON, returns error indicator (no data leak)."""
    from core.webhooks import _render_payload_template
    # A template that isn't JSON after rendering (not wrapped in {})
    result = _render_payload_template("not json {{key}}", {"key": "value"})
    assert result["error"] == "template_render_failed"
    # Must NOT contain the substituted context values
    assert "_raw" not in result
    assert "value" not in str(result)


def test_render_none_value_becomes_empty_string():
    """None value in context → empty string substituted."""
    from core.webhooks import _render_payload_template
    result = _render_payload_template('{"v": "{{k}}"}', {"k": None})
    assert result == {"v": ""}


# ── ALL_EVENTS / DEFAULT_EVENTS integrity ────────────────────────────────────

def test_all_events_no_duplicates():
    from core.webhooks import ALL_EVENTS
    assert len(ALL_EVENTS) == len(set(ALL_EVENTS)), "Duplicate event in ALL_EVENTS"


def test_all_events_follow_resource_action_pattern():
    """All event strings should be 'resource.action' format."""
    from core.webhooks import ALL_EVENTS
    for event in ALL_EVENTS:
        assert "." in event, f"Event '{event}' does not follow 'resource.action' pattern"
        parts = event.split(".")
        assert all(p for p in parts), f"Event '{event}' has empty segment"


def test_default_events_subset_of_all_events():
    from core.webhooks import ALL_EVENTS, DEFAULT_EVENTS
    all_set = set(ALL_EVENTS)
    for event in DEFAULT_EVENTS:
        assert event in all_set, f"DEFAULT_EVENTS contains unknown event: '{event}'"
