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
| 13 | Full human-task flow E2E tests (26 tests: create/claim/submit/approve/reject + negative paths) | 0950227 |
| 14 | Fix pre-existing test isolation: lru_cache settings leak + background task exception propagation | 0950227 |
| 15 | Fix stale test_task_endpoints tests (templates is public; public feed needs mock DB) | 0950227 |
| 16 | Migrate `on_event` → `lifespan` context manager in main.py (FastAPI deprecation) | 2d29ce2 |
| 17 | Fix Pydantic v2 warnings: `@validator` → `@field_validator` (availability.py), `regex=` → `pattern=` (analytics.py) | 2d29ce2 |

| 18 | Fix Pydantic v2 class Config → ConfigDict in 6 router files (availability, experiments, onboarding, reputation, sla, task_messages) | 7765b58 |
| 19 | Homepage: replace Rick Roll placeholder with interactive API demo terminal (3 scenarios, typewriter animation) | 7765b58 |
| 20 | Task detail: Markdown rendering for LLM output with Formatted/Raw toggle (marked.js) | 7765b58 |
| 21 | Dashboard: improved onboarding banner with credits count + rich empty state with task type tiles | d25e6aa |
| 22 | new-task form: ?preset=<type> URL param pre-selects task type from dashboard tiles | cb6dcfe |
| 23 | new-task form: inline field-level validation (blur + submit) for required fields, URL format, JSON syntax, webhook URL | 9683e3d |
| 24 | Task detail live bar: elapsed timer, worker progress counter, adaptive backoff polling, completion/failure animations | 9afbb14 |
| 25 | Task detail: Markdown rendering for web_research summary + web_intel report (marked.js, llm-prose CSS) | 3087426 |
| 26 | Bug fix: compare_rank worker task UI reads inp.items array (form builder format), not legacy option_a/option_b | 3cd8b0c |
| 27 | Worker home: task feed preview (3 matched tasks), recent activity (5 completions), condensed quick links (8 essentials) | 94d19f6 |
| 28 | UX: worker submission client-side validation + type-specific review display (stars/verdicts/labels) on review page | ecf2e9a |
| 29 | Dashboard: pending review counter + human-readable task type labels + worker progress on recent tasks list | adfd938 |

## Priorities for Next Session 🔜

1. **Deploy blockers** (owner-dependent, needs GitHub Secrets):
   - `NPM_TOKEN` for `@crowdsourcerer/sdk` publish
   - PyPI OIDC for Python package publish

2. **Worker skill recommendations** — personalized task-type recommendations + earnings potential based on history

3. **Stale warning ts(6133)** — many unused variables across pages (non-blocking warnings)

## Known Warnings (non-blocking)

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
