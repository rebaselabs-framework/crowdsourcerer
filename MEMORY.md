# Session Memory / Priorities

Auto-updated by autonomous sessions. Tracks what was done and what's next.

## Completed ✅

| # | Task | Commit |
|---|------|--------|
| 1 | Copy JSON button on task detail input/output panels | f04a72e |
| 2 | Onboarding cold-path fixes (cookie bugs, role selection, smart redirect, welcome banner) | 47e2874 |
| 3 | Backend startup-blocking bug fixes (FastAPI 204, SQLAlchemy reserved attr, passlib/bcrypt, ambiguous FK) | 2f9b920 |
| 4 | Integration tests — 28/28 passing mock-based tests for register, task auth, onboarding auth, JWT | 2f9b920 |

## Priorities for Next Session 🔜

1. **Onboarding completion rate admin funnel** — admin page showing a funnel:
   - Registered → started onboarding → step 1 → step 2 → completed
   - Data from `requester_onboarding` table; aggregate by step
   - Route: `/dashboard/admin/onboarding-funnel`
   - Backend: `/v1/admin/onboarding/funnel` endpoint

2. **SQLAlchemy relationship overlap warnings** — `configure_mappers()` emits SAWarning
   about `UserDB.quiz_attempts`, `DisputeEventDB.worker`, and `DisputeEventDB.actor`
   all copying `users.id → dispute_events.actor_id`. Fix by adding `overlaps=` param.

3. **Deploy blockers** (owner-dependent, needs GitHub Secrets):
   - `NPM_TOKEN` for `@crowdsourcerer/sdk` publish
   - PyPI OIDC for Python package publish
   - Coolify webhook URL for auto-deploy trigger

## Known Warnings (non-blocking)

- passlib DeprecationWarning about `crypt` — passlib itself is gone from the app now
  but the import warning may linger in test deps. Safe to ignore.
- `on_event` deprecation in `main.py` — migrate to `lifespan` when touching main.py
- `@validator` in `availability.py` — migrate to `@field_validator` when touching that file
- `regex=` in `analytics.py` — migrate to `pattern=` when touching that file
- Pydantic v1 class Config in various models — migrate to `ConfigDict` when touching

## Architecture Notes

- **Cookie name**: auth token is `cs_token` (httpOnly). Any page reading `Astro.cookies.get('token')` is broken.
- **Rate limiter**: `routers/auth.py` has its own `Limiter` instance (not the app-level one).
  Tests must reset `from routers.auth import limiter; limiter._storage.reset()` if they call `/register` 5+ times.
- **FastAPI 204 + `from __future__ import annotations`**: Must always set `response_model=None`
  explicitly on 204 routes in files that use future annotations.
