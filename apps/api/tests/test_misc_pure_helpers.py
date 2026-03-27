"""Tests for pure helper functions across several routers.

two_factor.py — _hash_backup_code, _create_pending_token, _verify_pending_token:
  1.  _hash_backup_code: returns 64-char hex string
  2.  _hash_backup_code: deterministic
  3.  _hash_backup_code: different codes → different hashes
  4.  _hash_backup_code: empty string → still a 64-char hash
  5.  _create_pending_token: returns a non-empty string
  6.  _create_pending_token: decoded sub == user_id
  7.  _create_pending_token: decoded typ == '2fa_pending'
  8.  _verify_pending_token: valid token returns user_id
  9.  _verify_pending_token: wrong typ returns None
  10. _verify_pending_token: expired token returns None
  11. _verify_pending_token: garbage string returns None
  12. _verify_pending_token: empty string returns None

export.py — _summarise, _fmt_dt:
  13. _summarise: None input returns None
  14. _summarise: simple dict returns JSON string
  15. _summarise: long dict truncated with '…' suffix
  16. _summarise: exact-length dict NOT truncated
  17. _summarise: non-JSON-serialisable value falls back to str[:max_len]
  18. _fmt_dt: None input returns None
  19. _fmt_dt: datetime returns ISO string

notifications.py — _default_prefs_dict, _prefs_to_dict:
  20. _default_prefs_dict: all expected keys present
  21. _default_prefs_dict: email_task_completed is True
  22. _default_prefs_dict: digest_frequency is 'weekly'
  23. _default_prefs_dict: updated_at is None
  24. _prefs_to_dict: all expected keys present
  25. _prefs_to_dict: updated_at is None when prefs.updated_at is None
  26. _prefs_to_dict: updated_at is ISO string when prefs.updated_at is set

search.py — _task_title, _extract_match_context:
  27. _task_title: with instructions → 'Type: snippet'
  28. _task_title: instructions > 60 chars → truncated with '…'
  29. _task_title: no instructions, 'prompt' input key used
  30. _task_title: no instructions, 'title' input key used
  31. _task_title: no instructions, no known input keys → 'Type Task'
  32. _task_title: no instructions, known key but non-string value ignored
  33. _extract_match_context: term=None returns None
  34. _extract_match_context: empty term returns None
  35. _extract_match_context: match in instructions → field='instructions'
  36. _extract_match_context: snippet contains surrounding context
  37. _extract_match_context: match in input JSON → field='input'
  38. _extract_match_context: match in output JSON → field='output'
  39. _extract_match_context: no match anywhere → field='type' fallback

triggers.py — _compute_next_fire, _trigger_to_out:
  40. _compute_next_fire: valid cron returns datetime
  41. _compute_next_fire: invalid cron string returns None
  42. _compute_next_fire: returned datetime is timezone-aware
  43. _compute_next_fire: with explicit 'after' datetime result > after
  44. _trigger_to_out: webhook type with token → webhook_url is set
  45. _trigger_to_out: schedule type → webhook_url is None
  46. _trigger_to_out: webhook type but token=None → webhook_url is None
  47. _trigger_to_out: base_url prefix included in webhook_url

comments.py — _comment_out:
  48. _comment_out: all expected top-level keys present
  49. _comment_out: author_name uses user.name when set
  50. _comment_out: author_name falls back to email prefix
  51. _comment_out: parent_id None when c.parent_id is None
  52. _comment_out: parent_id is str-UUID when c.parent_id is set
  53. _comment_out: edited_at is None when not edited
  54. _comment_out: edited_at is ISO string when edited

portfolio.py — _result_snippet:
  55. _result_snippet: output=None returns None
  56. _result_snippet: 'summary' key used when present
  57. _result_snippet: 'text' key used when present
  58. _result_snippet: 'result' key used when present
  59. _result_snippet: unknown keys → stringify fallback
  60. _result_snippet: truncated at max_chars
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://localhost/test")
os.environ.setdefault("JWT_SECRET",   "app-test-secret")
os.environ.setdefault("API_KEY_SALT", "app-test-salt")
os.environ.setdefault("DEBUG",        "true")

import pytest


# ── _hash_backup_code ─────────────────────────────────────────────────────────

def test_hash_backup_code_returns_64_char_hex():
    from routers.two_factor import _hash_backup_code
    result = _hash_backup_code("ABCD-EFGH")
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_hash_backup_code_deterministic():
    from routers.two_factor import _hash_backup_code
    assert _hash_backup_code("same_code") == _hash_backup_code("same_code")


def test_hash_backup_code_different_codes_differ():
    from routers.two_factor import _hash_backup_code
    assert _hash_backup_code("code_a") != _hash_backup_code("code_b")


def test_hash_backup_code_empty_string():
    from routers.two_factor import _hash_backup_code
    result = _hash_backup_code("")
    assert len(result) == 64
    # SHA256 of empty string
    expected = hashlib.sha256(b"").hexdigest()
    assert result == expected


# ── _create_pending_token / _verify_pending_token ─────────────────────────────

def test_create_pending_token_returns_string():
    from routers.two_factor import _create_pending_token
    token = _create_pending_token("user-123")
    assert isinstance(token, str)
    assert len(token) > 10


def test_create_pending_token_sub_claim():
    from routers.two_factor import _create_pending_token
    from jose import jwt
    token = _create_pending_token("user-abc")
    payload = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=["HS256"])
    assert payload["sub"] == "user-abc"


def test_create_pending_token_typ_claim():
    from routers.two_factor import _create_pending_token
    from jose import jwt
    token = _create_pending_token("user-abc")
    payload = jwt.decode(token, os.environ["JWT_SECRET"], algorithms=["HS256"])
    assert payload["typ"] == "2fa_pending"


def test_verify_pending_token_valid_returns_user_id():
    from routers.two_factor import _create_pending_token, _verify_pending_token
    token = _create_pending_token("user-xyz")
    assert _verify_pending_token(token) == "user-xyz"


def test_verify_pending_token_wrong_type_returns_none():
    from routers.two_factor import _verify_pending_token
    from jose import jwt
    # Encode a token with typ='access' instead of '2fa_pending'
    payload = {"sub": "user-123", "typ": "access", "exp": int(time.time()) + 300}
    token = jwt.encode(payload, os.environ["JWT_SECRET"], algorithm="HS256")
    assert _verify_pending_token(token) is None


def test_verify_pending_token_expired_returns_none():
    from routers.two_factor import _verify_pending_token
    from jose import jwt
    # Encode a token with exp in the past
    payload = {"sub": "user-123", "typ": "2fa_pending", "exp": int(time.time()) - 10}
    token = jwt.encode(payload, os.environ["JWT_SECRET"], algorithm="HS256")
    assert _verify_pending_token(token) is None


def test_verify_pending_token_garbage_returns_none():
    from routers.two_factor import _verify_pending_token
    assert _verify_pending_token("not.a.jwt.at.all") is None


def test_verify_pending_token_empty_string_returns_none():
    from routers.two_factor import _verify_pending_token
    assert _verify_pending_token("") is None


# ── _summarise ────────────────────────────────────────────────────────────────

def test_summarise_none_returns_none():
    from routers.export import _summarise
    assert _summarise(None) is None


def test_summarise_simple_dict_returns_json():
    from routers.export import _summarise
    result = _summarise({"key": "value"})
    assert result is not None
    assert "key" in result
    assert "value" in result


def test_summarise_long_dict_truncated():
    from routers.export import _summarise
    big = {"x": "a" * 300}
    result = _summarise(big, max_len=50)
    assert result is not None
    assert len(result) <= 51  # 50 chars + '…'
    assert result.endswith("…")


def test_summarise_exact_length_not_truncated():
    from routers.export import _summarise
    import json
    data = {"k": "v"}
    s = json.dumps(data)
    result = _summarise(data, max_len=len(s))
    assert result == s
    assert not result.endswith("…")


def test_summarise_non_serialisable_fallback():
    from routers.export import _summarise

    class _Unserializable:
        pass

    # default=str in json.dumps should handle this; but if it can't, falls back
    data = {"obj": _Unserializable()}
    result = _summarise(data)
    assert result is not None


# ── _fmt_dt ───────────────────────────────────────────────────────────────────

def test_fmt_dt_none_returns_none():
    from routers.export import _fmt_dt
    assert _fmt_dt(None) is None


def test_fmt_dt_datetime_returns_iso():
    from routers.export import _fmt_dt
    dt = datetime(2025, 6, 15, 12, 30, 0, tzinfo=timezone.utc)
    result = _fmt_dt(dt)
    assert result == dt.isoformat()
    assert "2025" in result


# ── _default_prefs_dict ───────────────────────────────────────────────────────

_EXPECTED_PREF_KEYS = {
    "email_task_completed", "email_task_failed", "email_submission_received",
    "email_worker_approved", "email_payout_update", "email_daily_challenge",
    "email_task_available", "email_referral_bonus", "email_sla_breach",
    "notif_task_events", "notif_submissions", "notif_payouts",
    "notif_gamification", "notif_system", "digest_frequency", "updated_at",
}


def test_default_prefs_dict_all_expected_keys():
    from routers.notifications import _default_prefs_dict
    result = _default_prefs_dict()
    assert _EXPECTED_PREF_KEYS.issubset(set(result.keys()))


def test_default_prefs_dict_email_task_completed_true():
    from routers.notifications import _default_prefs_dict
    assert _default_prefs_dict()["email_task_completed"] is True


def test_default_prefs_dict_digest_frequency_weekly():
    from routers.notifications import _default_prefs_dict
    assert _default_prefs_dict()["digest_frequency"] == "weekly"


def test_default_prefs_dict_updated_at_none():
    from routers.notifications import _default_prefs_dict
    assert _default_prefs_dict()["updated_at"] is None


# ── _prefs_to_dict ────────────────────────────────────────────────────────────

def _make_prefs_db(updated_at=None):
    """Build a minimal mock NotificationPreferencesDB."""
    m = MagicMock()
    m.email_task_completed = True
    m.email_task_failed = False
    m.email_submission_received = True
    m.email_worker_approved = True
    m.email_payout_update = True
    m.email_daily_challenge = False
    m.email_task_available = False
    m.email_referral_bonus = True
    m.email_sla_breach = True
    m.notif_task_events = True
    m.notif_submissions = True
    m.notif_payouts = True
    m.notif_gamification = True
    m.notif_system = True
    m.digest_frequency = "daily"
    m.updated_at = updated_at
    return m


def test_prefs_to_dict_all_expected_keys():
    from routers.notifications import _prefs_to_dict
    result = _prefs_to_dict(_make_prefs_db())
    assert _EXPECTED_PREF_KEYS.issubset(set(result.keys()))


def test_prefs_to_dict_updated_at_none_when_no_timestamp():
    from routers.notifications import _prefs_to_dict
    result = _prefs_to_dict(_make_prefs_db(updated_at=None))
    assert result["updated_at"] is None


def test_prefs_to_dict_updated_at_iso_when_set():
    from routers.notifications import _prefs_to_dict
    dt = datetime(2025, 3, 10, 8, 0, 0, tzinfo=timezone.utc)
    result = _prefs_to_dict(_make_prefs_db(updated_at=dt))
    assert result["updated_at"] == dt.isoformat()


# ── _task_title ───────────────────────────────────────────────────────────────

def _make_task(type_="text_annotation", instructions=None, input=None):
    t = MagicMock()
    t.type = type_
    t.task_instructions = instructions
    t.input = input or {}
    return t


def test_task_title_with_instructions():
    from routers.search import _task_title
    task = _make_task(instructions="Annotate the sentiment.")
    result = _task_title(task)
    assert result.startswith("Text Annotation: ")
    assert "Annotate the sentiment." in result


def test_task_title_instructions_truncated_at_60():
    from routers.search import _task_title
    long_instr = "x" * 80
    task = _make_task(instructions=long_instr)
    result = _task_title(task)
    assert result.endswith("…")
    # type + ': ' + 60 chars + '…'
    assert len(result) <= len("Text Annotation: ") + 61


def test_task_title_no_instructions_uses_prompt():
    from routers.search import _task_title
    task = _make_task(input={"prompt": "Describe the image"})
    result = _task_title(task)
    assert "Describe the image" in result


def test_task_title_no_instructions_uses_title_key():
    from routers.search import _task_title
    task = _make_task(input={"title": "My dataset row"})
    result = _task_title(task)
    assert "My dataset row" in result


def test_task_title_no_instructions_no_known_keys():
    from routers.search import _task_title
    task = _make_task(type_="image_classification", input={"foo": "bar"})
    result = _task_title(task)
    assert result == "Image Classification Task"


def test_task_title_known_key_non_string_ignored():
    from routers.search import _task_title
    task = _make_task(input={"prompt": 42, "title": "Valid title"})
    result = _task_title(task)
    # prompt is non-string so skipped; title is used
    assert "Valid title" in result


# ── _extract_match_context ────────────────────────────────────────────────────

def _make_search_task(type_="text_annotation", instructions=None, input=None, output=None):
    t = MagicMock()
    t.type = type_
    t.task_instructions = instructions
    t.input = input
    t.output = output
    return t


def test_extract_match_context_none_term_returns_none():
    from routers.search import _extract_match_context
    task = _make_search_task(instructions="Some text here")
    assert _extract_match_context(task, None) is None


def test_extract_match_context_empty_term_returns_none():
    from routers.search import _extract_match_context
    task = _make_search_task(instructions="Some text here")
    assert _extract_match_context(task, "") is None


def test_extract_match_context_match_in_instructions():
    from routers.search import _extract_match_context
    task = _make_search_task(instructions="Please annotate the sentiment of this tweet.")
    result = _extract_match_context(task, "sentiment")
    assert result is not None
    assert result["field"] == "instructions"
    assert "sentiment" in result["snippet"].lower()


def test_extract_match_context_snippet_contains_context():
    from routers.search import _extract_match_context
    # Term in middle of long instruction
    task = _make_search_task(
        instructions="Context before " + "KEYWORD" + " context after. " * 10
    )
    result = _extract_match_context(task, "keyword")
    assert result is not None
    assert "KEYWORD" in result["snippet"]


def test_extract_match_context_match_in_input():
    from routers.search import _extract_match_context
    task = _make_search_task(
        instructions=None,
        input={"query": "find the unicorn image"},
    )
    result = _extract_match_context(task, "unicorn")
    assert result is not None
    assert result["field"] == "input"
    assert "unicorn" in result["snippet"]


def test_extract_match_context_match_in_output():
    from routers.search import _extract_match_context
    task = _make_search_task(
        instructions=None,
        input={},
        output={"result": "The dragon was slayed successfully"},
    )
    result = _extract_match_context(task, "dragon")
    assert result is not None
    assert result["field"] == "output"
    assert "dragon" in result["snippet"]


def test_extract_match_context_fallback_to_type():
    from routers.search import _extract_match_context
    task = _make_search_task(
        type_="audio_transcription",
        instructions=None,
        input={"foo": "bar"},
        output=None,
    )
    result = _extract_match_context(task, "nonexistentterm")
    assert result is not None
    assert result["field"] == "type"
    assert "Audio Transcription" in result["snippet"]


# ── _compute_next_fire ────────────────────────────────────────────────────────

def test_compute_next_fire_valid_cron_returns_datetime():
    from routers.triggers import _compute_next_fire
    result = _compute_next_fire("0 9 * * *")  # daily at 9am
    assert isinstance(result, datetime)


def test_compute_next_fire_invalid_cron_returns_none():
    from routers.triggers import _compute_next_fire
    result = _compute_next_fire("not a valid cron expression")
    assert result is None


def test_compute_next_fire_timezone_aware():
    from routers.triggers import _compute_next_fire
    result = _compute_next_fire("*/5 * * * *")  # every 5 minutes
    assert result is not None
    assert result.tzinfo is not None


def test_compute_next_fire_respects_after_param():
    from routers.triggers import _compute_next_fire
    after = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    result = _compute_next_fire("0 * * * *", after=after)  # hourly
    assert result is not None
    assert result > after


# ── _trigger_to_out ───────────────────────────────────────────────────────────

def _make_trigger(trigger_type="webhook", webhook_token="tok123", pipeline_id=None):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.pipeline_id = pipeline_id or uuid.uuid4()
    t.trigger_type = trigger_type
    t.name = "My Trigger"
    t.is_active = True
    t.cron_expression = None
    t.webhook_token = webhook_token
    t.default_input = {}
    t.last_fired_at = None
    t.next_fire_at = None
    t.run_count = 0
    t.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return t


def test_trigger_to_out_webhook_url_set():
    from routers.triggers import _trigger_to_out
    trigger = _make_trigger(trigger_type="webhook", webhook_token="mytoken")
    out = _trigger_to_out(trigger, base_url="https://api.example.com")
    assert out.webhook_url is not None
    assert "mytoken" in out.webhook_url


def test_trigger_to_out_schedule_no_webhook_url():
    from routers.triggers import _trigger_to_out
    trigger = _make_trigger(trigger_type="schedule")
    out = _trigger_to_out(trigger, base_url="https://api.example.com")
    assert out.webhook_url is None


def test_trigger_to_out_webhook_token_none_no_url():
    from routers.triggers import _trigger_to_out
    trigger = _make_trigger(trigger_type="webhook", webhook_token=None)
    out = _trigger_to_out(trigger)
    assert out.webhook_url is None


def test_trigger_to_out_base_url_prefix():
    from routers.triggers import _trigger_to_out
    trigger = _make_trigger(trigger_type="webhook", webhook_token="tok42")
    out = _trigger_to_out(trigger, base_url="https://api.example.com")
    assert out.webhook_url.startswith("https://api.example.com")


# ── _comment_out ──────────────────────────────────────────────────────────────

def _make_comment(parent_id=None, edited_at=None):
    c = MagicMock()
    c.id = uuid.uuid4()
    c.task_id = uuid.uuid4()
    c.user_id = uuid.uuid4()
    c.parent_id = parent_id
    c.body = "This is a test comment."
    c.is_internal = False
    c.edited_at = edited_at
    c.created_at = datetime(2025, 5, 20, 10, 0, 0, tzinfo=timezone.utc)
    return c


def _make_author(name=None, email="author@example.com"):
    u = MagicMock()
    u.name = name
    u.email = email
    return u


def test_comment_out_all_keys_present():
    from routers.comments import _comment_out
    c = _make_comment()
    a = _make_author(name="Alice")
    result = _comment_out(c, a)
    for key in ("id", "task_id", "user_id", "author_name", "parent_id",
                "body", "is_internal", "edited_at", "created_at"):
        assert key in result, f"Missing key: {key}"


def test_comment_out_author_name_uses_name():
    from routers.comments import _comment_out
    c = _make_comment()
    a = _make_author(name="Bob")
    result = _comment_out(c, a)
    assert result["author_name"] == "Bob"


def test_comment_out_author_name_fallback_email():
    from routers.comments import _comment_out
    c = _make_comment()
    a = _make_author(name=None, email="charlie@example.com")
    result = _comment_out(c, a)
    assert result["author_name"] == "charlie"


def test_comment_out_parent_id_none_when_absent():
    from routers.comments import _comment_out
    c = _make_comment(parent_id=None)
    a = _make_author(name="Dave")
    assert _comment_out(c, a)["parent_id"] is None


def test_comment_out_parent_id_str_when_present():
    from routers.comments import _comment_out
    pid = uuid.uuid4()
    c = _make_comment(parent_id=pid)
    a = _make_author(name="Eve")
    result = _comment_out(c, a)["parent_id"]
    assert result == str(pid)


def test_comment_out_edited_at_none_when_unedited():
    from routers.comments import _comment_out
    c = _make_comment(edited_at=None)
    a = _make_author(name="Frank")
    assert _comment_out(c, a)["edited_at"] is None


def test_comment_out_edited_at_iso_when_set():
    from routers.comments import _comment_out
    dt = datetime(2025, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
    c = _make_comment(edited_at=dt)
    a = _make_author(name="Grace")
    assert _comment_out(c, a)["edited_at"] == dt.isoformat()


# ── _result_snippet ───────────────────────────────────────────────────────────

def _make_portfolio_task(output=None):
    t = MagicMock()
    t.output = output
    return t


def test_result_snippet_no_output_returns_none():
    from routers.portfolio import _result_snippet
    t = _make_portfolio_task(output=None)
    assert _result_snippet(t) is None


def test_result_snippet_summary_key_used():
    from routers.portfolio import _result_snippet
    t = _make_portfolio_task(output={"summary": "Great work done here."})
    result = _result_snippet(t)
    assert result == "Great work done here."


def test_result_snippet_text_key_used():
    from routers.portfolio import _result_snippet
    t = _make_portfolio_task(output={"text": "Transcription complete."})
    result = _result_snippet(t)
    assert result == "Transcription complete."


def test_result_snippet_result_key_used():
    from routers.portfolio import _result_snippet
    t = _make_portfolio_task(output={"result": "Classification: cat"})
    result = _result_snippet(t)
    assert result == "Classification: cat"


def test_result_snippet_unknown_keys_fallback():
    from routers.portfolio import _result_snippet
    t = _make_portfolio_task(output={"custom_key": "custom_value"})
    result = _result_snippet(t)
    assert result is not None
    assert "custom_key" in result or "custom_value" in result


def test_result_snippet_truncated_at_max_chars():
    from routers.portfolio import _result_snippet
    t = _make_portfolio_task(output={"summary": "x" * 300})
    result = _result_snippet(t, max_chars=50)
    assert result is not None
    assert len(result) == 50
