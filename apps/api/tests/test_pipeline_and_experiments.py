"""Unit tests for pure helper functions in pipelines.py and experiments.py.

routers/pipelines.py — _extract_path:
  1.  Non-$ path → returns literal (passthrough)
  2.  $.input.key → value from pipeline_input
  3.  $.input.nested.key → deep dict traversal
  4.  $.input.missing → None
  5.  $.steps.0.key → value from step_outputs[0][key]
  6.  $.steps.1.key → value from step_outputs[1][key]
  7.  Step index out of range → None
  8.  Non-integer step index → None
  9.  $.steps (no sub-key) → None
  10. $.steps.0 with None output → {} (empty dict access)
  11. Nested multi-level step key traversal
  12. Unknown root segment → None
  13. Empty path string → passthrough (not $-prefixed)
  14. $.input with empty pipeline_input → None for missing key

routers/pipelines.py — _evaluate_condition:
  15. None condition → True
  16. Empty string → True
  17. "true" literal → True
  18. "false" literal → False
  19. "TRUE" (case insensitive) → True
  20. Path truthy value → True
  21. Path falsy value (0, None) → False
  22. String equality: $.input.type == "web_research" → True
  23. String equality: $.input.type == "wrong_type" → False
  24. Numeric >: score 0.9 > 0.8 → True
  25. Numeric >: score 0.7 > 0.8 → False
  26. Numeric >=: score 0.8 >= 0.8 → True
  27. Numeric <: score 0.5 < 0.8 → True
  28. Numeric <=: score 0.8 <= 0.8 → True
  29. Inequality !=: "a" != "b" → True
  30. Unknown expression → True (pass through)
  31. Invalid type coercion → False

routers/pipelines.py — _resolve_input:
  32. No mapping → static_config + pipeline_input merged
  33. No mapping + step_outputs → adds "prev" key from last non-None output
  34. With mapping → maps only specified keys
  35. Mapped key from $.input path
  36. Missing path in mapping → key omitted (None not inserted)

routers/experiments.py — _chi_squared_p:
  37. Not exactly 2 variants → None
  38. Insufficient data (< 5 per cell) → None
  39. Equal completion rates → p close to 1.0 (no significant difference)
  40. Very different rates → p < 0.05 (statistically significant)
  41. All completed (no failures) → None (zero in denominator)

routers/experiments.py — _pick_winner:
  42. Empty variants list → None
  43. All zero participants → None
  44. completion_rate metric: picks highest rate
  45. accuracy metric: picks highest accuracy rate
  46. avg_time metric: picks fastest (lowest time)
  47. credits_used metric: picks cheapest (lowest credits)
  48. Unknown metric → None
  49. accuracy with no completions → None

routers/experiments.py — _make_recommendation:
  50. < 20 participants → insufficient data message
  51. winner_id=None → "No clear winner yet."
  52. winner_id found but p_value is None → leads message
  53. winner p_value < 0.05 → statistically significant message
  54. winner p_value >= 0.05 → not yet significant message
  55. winner_id not in variants list → fallback message
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")


# ── Test helpers ──────────────────────────────────────────────────────────────

def _make_variant(
    id=None,
    name="Control",
    participant_count=100,
    completion_count=80,
    failure_count=20,
    total_accuracy=72.0,
    total_duration_ms=50000.0,
    total_credits_used=200.0,
    traffic_pct=50.0,
    is_control=True,
):
    v = MagicMock()
    v.id = id or uuid.uuid4()
    v.name = name
    v.participant_count = participant_count
    v.completion_count = completion_count
    v.failure_count = failure_count
    v.total_accuracy = total_accuracy
    v.total_duration_ms = total_duration_ms
    v.total_credits_used = total_credits_used
    v.traffic_pct = traffic_pct
    v.is_control = is_control
    return v


# ── _extract_path ─────────────────────────────────────────────────────────────

def test_extract_path_literal_passthrough():
    """Non-$ path string is returned as-is (literal value)."""
    from routers.pipelines import _extract_path
    assert _extract_path("hello_world", {}, []) == "hello_world"


def test_extract_path_from_input():
    """$.input.key extracts from pipeline_input."""
    from routers.pipelines import _extract_path
    result = _extract_path("$.input.text", {"text": "hello"}, [])
    assert result == "hello"


def test_extract_path_deep_input():
    """$.input.a.b traverses nested dicts."""
    from routers.pipelines import _extract_path
    result = _extract_path("$.input.meta.source", {"meta": {"source": "web"}}, [])
    assert result == "web"


def test_extract_path_missing_input_key():
    """Missing key in pipeline_input → None."""
    from routers.pipelines import _extract_path
    result = _extract_path("$.input.missing", {"other": 1}, [])
    assert result is None


def test_extract_path_step_output():
    """$.steps.0.key extracts from step_outputs[0]."""
    from routers.pipelines import _extract_path
    result = _extract_path("$.steps.0.score", {}, [{"score": 0.9}])
    assert result == 0.9


def test_extract_path_second_step():
    """$.steps.1.result extracts from step_outputs[1]."""
    from routers.pipelines import _extract_path
    result = _extract_path("$.steps.1.label", {}, [None, {"label": "cat"}])
    assert result == "cat"


def test_extract_path_step_index_out_of_range():
    """Step index beyond list length → None."""
    from routers.pipelines import _extract_path
    result = _extract_path("$.steps.5.key", {}, [{"key": "x"}])
    assert result is None


def test_extract_path_non_integer_step_index():
    """Non-integer step index (e.g. 'last') → None."""
    from routers.pipelines import _extract_path
    result = _extract_path("$.steps.last.key", {}, [{"key": "x"}])
    assert result is None


def test_extract_path_steps_no_subkey():
    """$.steps with no further key → None."""
    from routers.pipelines import _extract_path
    result = _extract_path("$.steps", {}, [{"x": 1}])
    assert result is None


def test_extract_path_none_step_output_treated_as_empty():
    """None step output → {} access → None for any key."""
    from routers.pipelines import _extract_path
    result = _extract_path("$.steps.0.key", {}, [None])
    assert result is None


def test_extract_path_nested_step_key():
    """$.steps.0.output.nested_key traverses multi-level."""
    from routers.pipelines import _extract_path
    result = _extract_path("$.steps.0.output.label", {}, [{"output": {"label": "dog"}}])
    assert result == "dog"


def test_extract_path_unknown_root():
    """Unknown root segment (not 'input' or 'steps') → None."""
    from routers.pipelines import _extract_path
    result = _extract_path("$.config.key", {"config": {"key": "x"}}, [])
    assert result is None


def test_extract_path_empty_string_passthrough():
    """Empty string path (doesn't start with $) → passthrough."""
    from routers.pipelines import _extract_path
    result = _extract_path("", {}, [])
    assert result == ""


def test_extract_path_empty_input_missing_key():
    """Empty pipeline_input with key path → None."""
    from routers.pipelines import _extract_path
    result = _extract_path("$.input.key", {}, [])
    assert result is None


# ── _evaluate_condition ───────────────────────────────────────────────────────

def test_evaluate_condition_none_returns_true():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition(None, {}, []) is True


def test_evaluate_condition_empty_string_returns_true():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition("", {}, []) is True


def test_evaluate_condition_whitespace_only_returns_true():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition("   ", {}, []) is True


def test_evaluate_condition_literal_true():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition("true", {}, []) is True


def test_evaluate_condition_literal_false():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition("false", {}, []) is False


def test_evaluate_condition_case_insensitive_true():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition("TRUE", {}, []) is True


def test_evaluate_condition_truthy_path():
    """Path to a truthy value → True."""
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition("$.input.active", {"active": True}, []) is True


def test_evaluate_condition_falsy_path_zero():
    """Path to 0 → bool(0) == False."""
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition("$.input.count", {"count": 0}, []) is False


def test_evaluate_condition_string_equality_true():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition(
        '$.input.type == "web_research"',
        {"type": "web_research"},
        [],
    ) is True


def test_evaluate_condition_string_equality_false():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition(
        '$.input.type == "web_research"',
        {"type": "other"},
        [],
    ) is False


def test_evaluate_condition_numeric_gt_true():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition(
        "$.steps.0.score > 0.8",
        {},
        [{"score": 0.9}],
    ) is True


def test_evaluate_condition_numeric_gt_false():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition(
        "$.steps.0.score > 0.8",
        {},
        [{"score": 0.7}],
    ) is False


def test_evaluate_condition_numeric_gte_equal():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition(
        "$.steps.0.score >= 0.8",
        {},
        [{"score": 0.8}],
    ) is True


def test_evaluate_condition_numeric_lt():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition(
        "$.steps.0.score < 0.8",
        {},
        [{"score": 0.5}],
    ) is True


def test_evaluate_condition_numeric_lte_equal():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition(
        "$.steps.0.score <= 0.8",
        {},
        [{"score": 0.8}],
    ) is True


def test_evaluate_condition_inequality_ne():
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition(
        '$.input.lang != "en"',
        {"lang": "fr"},
        [],
    ) is True


def test_evaluate_condition_unknown_expression_passes():
    """Non-$ non-keyword expression → True (pass through)."""
    from routers.pipelines import _evaluate_condition
    assert _evaluate_condition("some_unknown_thing", {}, []) is True


# ── _resolve_input ────────────────────────────────────────────────────────────

def test_resolve_input_no_mapping_merges_input():
    """No mapping: static_config + pipeline_input merged."""
    from routers.pipelines import _resolve_input
    result = _resolve_input(None, {"static": 1}, {"dynamic": 2}, [])
    assert result["static"] == 1
    assert result["dynamic"] == 2


def test_resolve_input_no_mapping_adds_prev():
    """No mapping + step_outputs → 'prev' key from last non-None output."""
    from routers.pipelines import _resolve_input
    result = _resolve_input(None, {}, {}, [{"out": "x"}, {"out": "y"}])
    assert result["prev"] == {"out": "y"}


def test_resolve_input_no_mapping_none_outputs_no_prev():
    """No mapping + all-None step_outputs → no 'prev' key."""
    from routers.pipelines import _resolve_input
    result = _resolve_input(None, {}, {}, [None, None])
    assert "prev" not in result


def test_resolve_input_with_mapping():
    """With mapping: only specified keys are populated."""
    from routers.pipelines import _resolve_input
    mapping = {"text": "$.input.raw_text"}
    result  = _resolve_input(mapping, {"static_k": "v"}, {"raw_text": "hello"}, [])
    assert result["text"] == "hello"
    assert result["static_k"] == "v"


def test_resolve_input_missing_path_not_included():
    """Mapped path that resolves to None is not included in result."""
    from routers.pipelines import _resolve_input
    mapping = {"missing": "$.input.does_not_exist"}
    result  = _resolve_input(mapping, {}, {"other": 1}, [])
    assert "missing" not in result


# ── _chi_squared_p ────────────────────────────────────────────────────────────

def test_chi_squared_p_not_two_variants_returns_none():
    from routers.experiments import _chi_squared_p
    assert _chi_squared_p([]) is None
    assert _chi_squared_p([(10, 20)]) is None
    assert _chi_squared_p([(10, 20), (15, 25), (8, 18)]) is None


def test_chi_squared_p_insufficient_data_returns_none():
    """Less than 5 in any cell → None."""
    from routers.experiments import _chi_squared_p
    # c1=4 < 5 → insufficient
    result = _chi_squared_p([(4, 100), (50, 100)])
    assert result is None


def test_chi_squared_p_equal_rates_high_p():
    """Identical completion rates → high p (not significant)."""
    from routers.experiments import _chi_squared_p
    result = _chi_squared_p([(50, 100), (50, 100)])
    assert result is not None
    assert result > 0.8  # not significant


def test_chi_squared_p_very_different_rates_low_p():
    """Very different completion rates → low p (statistically significant)."""
    from routers.experiments import _chi_squared_p
    result = _chi_squared_p([(90, 100), (10, 100)])
    assert result is not None
    assert result < 0.05


def test_chi_squared_p_all_completed_returns_none():
    """All tasks completed → no failures → zero expected cell → None."""
    from routers.experiments import _chi_squared_p
    result = _chi_squared_p([(100, 100), (100, 100)])
    assert result is None


def test_chi_squared_p_returns_float_in_0_1():
    """When sufficient data, result is a float in [0, 1]."""
    from routers.experiments import _chi_squared_p
    result = _chi_squared_p([(50, 100), (80, 100)])
    assert result is not None
    assert isinstance(result, float)
    assert 0.0 <= result <= 1.0


# ── _pick_winner ──────────────────────────────────────────────────────────────

def test_pick_winner_empty_list():
    from routers.experiments import _pick_winner
    assert _pick_winner([], "completion_rate") is None


def test_pick_winner_all_zero_participants():
    from routers.experiments import _pick_winner
    v1 = _make_variant(participant_count=0, completion_count=0)
    v2 = _make_variant(participant_count=0, completion_count=0)
    assert _pick_winner([v1, v2], "completion_rate") is None


def test_pick_winner_completion_rate():
    """Picks variant with highest completion_count / participant_count."""
    from routers.experiments import _pick_winner
    winner_id = uuid.uuid4()
    v1 = _make_variant(id=winner_id, participant_count=100, completion_count=90)  # 90%
    v2 = _make_variant(participant_count=100, completion_count=60)                # 60%
    assert _pick_winner([v1, v2], "completion_rate") == winner_id


def test_pick_winner_accuracy():
    """Picks variant with highest total_accuracy / completion_count."""
    from routers.experiments import _pick_winner
    winner_id = uuid.uuid4()
    v1 = _make_variant(id=winner_id, completion_count=50, total_accuracy=45.0)  # 0.9
    v2 = _make_variant(completion_count=50, total_accuracy=35.0)                 # 0.7
    assert _pick_winner([v1, v2], "accuracy") == winner_id


def test_pick_winner_avg_time():
    """For avg_time, picks the variant with LOWEST average duration."""
    from routers.experiments import _pick_winner
    winner_id = uuid.uuid4()
    v1 = _make_variant(id=winner_id, completion_count=50, total_duration_ms=10000.0)  # 200ms
    v2 = _make_variant(completion_count=50, total_duration_ms=50000.0)                 # 1000ms
    assert _pick_winner([v1, v2], "avg_time") == winner_id


def test_pick_winner_credits_used():
    """For credits_used, picks the variant with LOWEST average credits."""
    from routers.experiments import _pick_winner
    winner_id = uuid.uuid4()
    v1 = _make_variant(id=winner_id, completion_count=50, total_credits_used=50.0)  # 1/task
    v2 = _make_variant(completion_count=50, total_credits_used=200.0)                # 4/task
    assert _pick_winner([v1, v2], "credits_used") == winner_id


def test_pick_winner_unknown_metric():
    from routers.experiments import _pick_winner
    v1 = _make_variant()
    assert _pick_winner([v1], "unknown_metric") is None


def test_pick_winner_accuracy_no_completions():
    """accuracy metric with no completions → None."""
    from routers.experiments import _pick_winner
    v1 = _make_variant(completion_count=0, total_accuracy=0.0)
    v2 = _make_variant(completion_count=0, total_accuracy=0.0)
    assert _pick_winner([v1, v2], "accuracy") is None


# ── _make_recommendation ─────────────────────────────────────────────────────

def test_make_recommendation_insufficient_data():
    from routers.experiments import _make_recommendation
    v1 = _make_variant(participant_count=5)
    v2 = _make_variant(participant_count=3)
    result = _make_recommendation([v1, v2], "completion_rate", None, None)
    assert "Insufficient data" in result
    assert "8" in result  # total = 8


def test_make_recommendation_no_winner():
    from routers.experiments import _make_recommendation
    v1 = _make_variant(participant_count=15)
    v2 = _make_variant(participant_count=15)
    result = _make_recommendation([v1, v2], "completion_rate", None, None)
    assert "No clear winner" in result


def test_make_recommendation_winner_no_p_value():
    """Winner found but no p_value → 'leads on...' message."""
    from routers.experiments import _make_recommendation
    winner_id = uuid.uuid4()
    v1 = _make_variant(id=winner_id, name="Variant B", participant_count=50)
    v2 = _make_variant(participant_count=50)
    result = _make_recommendation([v1, v2], "completion_rate", None, winner_id)
    assert "Variant B" in result
    assert "statistical significance" in result.lower() or "longer" in result


def test_make_recommendation_statistically_significant():
    """p_value < 0.05 → significant winner message."""
    from routers.experiments import _make_recommendation
    winner_id = uuid.uuid4()
    v1 = _make_variant(id=winner_id, name="Winner", participant_count=100)
    v2 = _make_variant(participant_count=100)
    result = _make_recommendation([v1, v2], "completion_rate", 0.01, winner_id)
    assert "statistically significant" in result
    assert "Winner" in result


def test_make_recommendation_not_yet_significant():
    """p_value >= 0.05 → not yet significant message."""
    from routers.experiments import _make_recommendation
    winner_id = uuid.uuid4()
    v1 = _make_variant(id=winner_id, name="Leader", participant_count=100)
    v2 = _make_variant(participant_count=100)
    result = _make_recommendation([v1, v2], "completion_rate", 0.15, winner_id)
    assert "not yet" in result.lower() or "continue" in result.lower()
    assert "Leader" in result


def test_make_recommendation_winner_not_in_list():
    """winner_id that doesn't match any variant → fallback message."""
    from routers.experiments import _make_recommendation
    v1 = _make_variant(participant_count=50)
    v2 = _make_variant(participant_count=50)
    unknown_id = uuid.uuid4()
    result = _make_recommendation([v1, v2], "completion_rate", 0.01, unknown_id)
    assert "No winner" in result or "winner" in result.lower()
