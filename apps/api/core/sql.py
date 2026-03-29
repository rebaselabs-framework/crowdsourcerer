"""Shared SQL helpers used across multiple routers."""

# ── ILIKE / LIKE escape ──────────────────────────────────────────────────────
# Backslash is used as the LIKE escape character in PostgreSQL.
# These are exposed as public symbols so routers can import them:
#
#   from core.sql import esc_like, LIKE_ESC
#
#   term = f"%{esc_like(user_input)}%"
#   col.ilike(term, escape=LIKE_ESC)

LIKE_ESC = "\\"


def esc_like(s: str) -> str:
    """Escape ``%``, ``_``, and ``\\`` so user input is treated literally in LIKE / ILIKE."""
    return (
        s
        .replace(LIKE_ESC, LIKE_ESC * 2)
        .replace("%", f"{LIKE_ESC}%")
        .replace("_", f"{LIKE_ESC}_")
    )
