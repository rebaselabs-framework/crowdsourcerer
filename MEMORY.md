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
| 7 | Guided task-creation form builder with type-specific fields for all 18 task types | cc01dd3 |
| 8 | Dark-theme all 22 light-mode pages (bg-white cards/modals → dark design system) | 90c7094, e4cdd3c |
| 9 | Fix quota.astro: wrong API endpoint + env var + migrate to apiFetch | 90c7094 |
| 10 | Worker onboarding funnel admin page (`/admin/worker-onboarding-funnel` + `GET /v1/admin/worker-onboarding/funnel`) | e4cdd3c |
| 11 | Extend integration tests to 43 (requester onboarding flow, worker onboarding, claim/submit guards) | 49af1eb |
| 12 | Bug fix: explicitly set SQLAlchemy boolean defaults in `_get_or_create` for both onboarding routers | 49af1eb |

## Priorities for Next Session 🔜

1. **Full task flow E2E test** — the happy path mock test for human task:
   - POST /v1/tasks (human type) → 201 with task_id
   - POST /v1/worker/tasks/{id}/claim → 200 with assignment
   - POST /v1/worker/tasks/{id}/submit → 200
   - POST /v1/tasks/{id}/submissions/{assign_id}/approve → 200
   Needs careful mocking of: quota enforcement, credit deduction, worker stats,
   assignment creation, consensus logic. Use `side_effect` with call counter.

2. **Deploy blockers** (owner-dependent, needs GitHub Secrets):
   - `NPM_TOKEN` for `@crowdsourcerer/sdk` publish
   - PyPI OIDC for Python package publish
   - Coolify webhook URL for auto-deploy trigger

3. **Known Pydantic warnings to fix** (touch files opportunistically):
   - `on_event` deprecation in `main.py` → migrate to `lifespan`
   - `@validator` in `availability.py` → `@field_validator`
   - `regex=` in `analytics.py` → `pattern=`
   - Pydantic v1 class Config → `ConfigDict`

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
- **SQLAlchemy boolean defaults**: `Column(Boolean, default=False)` only sets SQL-level DEFAULT. Python objects have `None` until refreshed from DB. Always set boolean fields explicitly in `_get_or_create` helpers.
- **Tailwind dark mode**: App has NO `darkMode: 'class'` in tailwind.config.js. All `dark:` variant classes are dead code. Use explicit dark colors only.
- **Dark theme design system**: body = `bg-gray-950`, cards = `bg-gray-900 border border-gray-800` or `bg-gray-800 border border-gray-700`, primary accent = `violet-600`, text = `text-gray-100`/`text-gray-300`/`text-gray-400`.
