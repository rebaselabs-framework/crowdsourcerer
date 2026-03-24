# Session Memory / Priorities

Auto-updated by autonomous sessions. Tracks what was done and what's next.

## Completed ✅

| # | Task | Commit |
|---|------|--------|
| 1 | Copy JSON button on task detail input/output panels | f04a72e |
| 2 | Onboarding cold-path fixes (cookie bugs, role selection, smart redirect, welcome banner) | 47e2874 |
| 3 | Backend startup-blocking bug fixes (FastAPI 204, SQLAlchemy reserved attr, passlib/bcrypt, ambiguous FK) | 2f9b920 |
| 4 | Integration tests — 28/28 passing mock-based tests for register, task auth, onboarding auth, JWT | 2f9b920 |
| 5 | Onboarding completion rate admin funnel (`/admin/onboarding-funnel` + `GET /v1/admin/onboarding/funnel`) | 4324593 |
| 6 | Remove stray `DisputeEventDB.worker` relationship eliminating SQLAlchemy SAWarnings | 419a09a |

## Priorities for Next Session 🔜

1. **E2E test coverage** — extend `test_integration.py` with more flow tests:
   - Full task create → submit → approve happy path (mock DB)
   - Requester onboarding step completion flow
   - Worker assignment claim/submit flow

2. **Worker onboarding funnel** — similar to requester funnel at `/admin/onboarding-funnel`
   but for worker onboarding steps; check `WorkerOnboardingDB` model for available steps.

3. **Deploy blockers** (owner-dependent, needs GitHub Secrets):
   - `NPM_TOKEN` for `@crowdsourcerer/sdk` publish
   - PyPI OIDC for Python package publish
   - Coolify webhook URL for auto-deploy trigger

## Known Warnings (non-blocking)

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
- **bcrypt**: passlib removed; use `import bcrypt as _bcrypt` directly. `_bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()` and `_bcrypt.checkpw(pw.encode(), hash.encode())`.
- **SQLAlchemy ambiguous FK**: When a model has multiple FKs to the same table, always add `foreign_keys=` to relationships.
