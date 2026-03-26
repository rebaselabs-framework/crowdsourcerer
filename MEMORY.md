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
| 30 | Worker skill recommendations: `GET /v1/worker/recommendations` + `/worker/recommendations` page — best_types, try_next, insights, weekly earnings potential | 218ea95 |
| 31 | Password reset flow: forgot-password + reset-password pages, POST /v1/auth/forgot-password + /reset-password endpoints, SHA-256 token hashing, 30-min TTL, email enumeration prevention | c17e4c4 |
| 32 | Change password (logged-in): POST /v1/auth/change-password endpoint + security.astro card + Astro API proxy | c17e4c4 |
| 33 | Task text search: `q` query param on GET /v1/tasks, ILIKE on task_instructions + cast(input, Text), search UI on tasks list | a12dc24 |
| 34 | Worker marketplace improved empty state: contextual messaging, mode-switch CTAs, add-skills CTA, 8-task-type showcase grid | 4b670e0 |
| 35 | Low credit balance email: notify_low_credits() + HTML template in email.py, wired into maybe_fire_credit_alert() via asyncio.ensure_future | 69b8136 |
| 36 | Email verification for new signups (migration 0043) — 24h link, resend endpoint, amber banner | 87fd723 |
| 37 | Google OAuth login + signup (migration 0044) — social login, auto-link by email, skip verify step | d2a555e |
| 38 | Admin setup checklist `/admin/setup` — DB/JWT/RebaseKit/Email/Stripe/OAuth/cache status | effc35b |
| 39 | Worker task-available email notifications (migration 0045) — opt-in email when new human task matches skills | 4d54f2d |
| 40 | Fix 28 ts(6133) unused-variable hints across Astro pages | f98cded |
| 41 | SSR perf: withTimeout() utility + apply to requester hub, worker home, analytics (2–3s cap on heavy calls) | 2397d9d |
| 42 | Reduce Astro check hints 54 → 5 (91% reduction): is:inline on define:vars, event delegation, data-confirm pattern, optional chaining, clipboard API | 2c0f79a |
| 43 | Public task marketplace at /marketplace (public, no auth) + 301 redirect from /tasks + nav link update | 3b033b1 |
| 44 | Task launched banner on task detail (mode-aware: AI=green/queued, human=violet/live), dismiss via history.replaceState | 4dd3b69 |
| 45 | Fix missing DELETE /api/notifications/[id] Astro proxy route (per-notification delete was silently 404ing) | 4dd3b69 |
| 46 | Per-minute burst rate limiting on task creation: free=3/min, starter=10/min, pro=30/min, enterprise=100/min | 4dd3b69 |
| 47 | Marketplace text search + sort + reward filter (API + UI) + Save search CTA + /worker/saved-searches page | d688a1b |
| 48 | Admin health: stuck task detection (AI >30m, human >24h, timed-out assignments) + health page stuck panel | d688a1b |

## Session 2026-03-26 — Quality audit, 5 real bugs fixed (commit 642cdb9)

- **Sweeper N+1 fixed**: `sweep_once()` was doing O(N) DB queries per expired assignment; now loads all workers/tasks/requesters in 4 bulk queries. Same fix in `_sweep_sla_breaches()` (user plan lookup was per-task).
- **Stripe double-credit fixed**: `/v1/credits/webhook` lacked idempotency — replayed events would credit users twice. Added `StripeEventLogDB` guard matching the proper handler.
- **Task claim race condition fixed**: Added `SELECT ... FOR UPDATE` on task row in `claim_task()` to serialise concurrent claims.
- **Silent exceptions replaced with logging**: All `except Exception: pass` in worker.py now use `logger.warning()`.
- **Wrong test assertions fixed**: test_openapi_schema had `/v1/auth/token` (wrong) and `/openapi.json` (include_in_schema=False).

## Session 2026-03-26 (continued) — Silent failures + credit atomicity (commits 258edfe, a823bd6, 67b2f3b)

- **All `except Exception: pass` eliminated** from entire API codebase (was 22+ across tasks.py + 9 other files).
  Every silent swallow replaced with `logger.warning()` or `logger.error()` with `exc_info=True`.
  Critical stuck-run paths (tasks.py _run_task error recovery, pipelines.py run recovery) now log at ERROR.
- **Batch credit atomicity fixed**: `create_tasks_batch` deducted `total_credits` upfront but never refunded
  credits for tasks that failed in the loop. Added `actual_credits_charged` tracking + partial refund before commit.
- **Credit tests added** (5 new): `_calc_credits` unit tests, batch partial-refund logic, all-succeed, all-fail.
- **Test count**: 124 → 129 passing.

## Session 2026-03-26 (continued) — Deep quality audit round 2 (commits cfa57bd–0ad6370)

**Bugs fixed this round:**
- **_run_task double-invocation guard**: No status check before executing — if called twice, task ran twice. Added `if task.status != "queued": return` guard (cfa57bd).
- **4 more N+1 queries in tasks.py**: list_submissions (N worker lookups), bulk_task_action, bulk_cancel_tasks, bulk_archive_tasks all had per-ID queries in loops. All replaced with `.in_()` bulk loads (c9cb063).
- **Pipeline double-execution**: `_execute_pipeline_run` only checked for `cancelled`, not `completed`/`failed`. Re-runs after restart could double-execute completed pipelines (4c5d7a2).
- **Sweeper scheduled tasks race**: `_sweep_scheduled_tasks` had no row locking — two concurrent sweeper processes could both activate the same scheduled task. Added `.with_for_update(skip_locked=True)` (4c5d7a2).
- **Sweeper timeout race**: `sweep_once` had same issue with expired assignments. Added `.with_for_update(skip_locked=True)` (4c5d7a2).
- **Weekly digest N+1 bomb**: 5 per-user DB queries × N users. Replaced with 5 bulk GROUP BY queries. Would have caused OOM/timeout at scale (0ad6370).
- **Admin task queue unbounded**: No LIMIT on queue fetch. Added `.limit(500)` (0ad6370).

**Confirmed clean (no issues):**
- GET /v1/tasks list endpoint: clean, 2 queries total
- worker.py marketplace feed: no N+1s
- Double-charging in _run_task: credits charged once at task creation, refunds on cache hit are correct

## Session 2026-03-26 (continued) — N+1 sweep across all routers (commits 460a9ce–40e232f)

**All N+1 and unbounded fetch issues now resolved across the entire API codebase.**

Fixed this round:
- **sla.py list_sla_breaches**: Per-breach task title lookups → bulk IN load
- **quality.py** (4 fixes): `_update_accuracy` loaded all rows to count, now uses COUNT/SUM aggregate; `evaluate_submissions` per-worker load → bulk IN; `get_quality_report` broken `func.cast()` aggregate + per-worker follow-up queries → single correct GROUP BY + bulk worker IN; unbounded gold tasks query → `.limit(500)`
- **analytics.py org_analytics**: 2N per-member queries → 2 bulk GROUP BY queries
- **analytics.py completion_times**: Unbounded task fetch → `.limit(10_000)` safety cap
- **reputation.py recalculate_all**: 2N cert+strike queries → 2 bulk GROUP BY pre-loads; `compute_reputation()` now accepts optional pre-loaded data via `_cert_count`/`_strike_severities` kwargs
- **experiments.py list_experiments**: Per-experiment variant load → single bulk IN + dict; added `.limit(200)`
- **certifications.py list_certifications**: Per-cert question count → single GROUP BY
- **orgs.py list_my_orgs**: Per-org member count via `_org_to_out()` → bulk GROUP BY pre-load; `_org_to_out` accepts optional `member_count` kwarg
- **webhooks.py list_endpoints**: Added `.limit(100)`
- **admin.py update_user**: `body: dict` → typed `AdminUpdateUserRequest` with Pydantic bounds on credits
- **DB indexes migration 0046**: 8 new indexes on task_assignments (status, timeout_at, submitted_at, composite), tasks (execution_mode, type, pending+scheduled composite), credit_transactions (created_at)

**Zero remaining `except Exception: pass` silent swallows in codebase.**

## Session 2026-03-26 (continued) — Credits hardening, webhook fixes, more N+1 (commits 40e232f–faffd4d)

Fixed this round:
- **certifications/orgs/webhooks N+1**: certifications question count loop → GROUP BY; orgs member count loop → GROUP BY via optional kwarg on `_org_to_out`; webhook list endpoint got `.limit(100)`
- **experiments N+1**: list_experiments variant loop → bulk IN load; added `.limit(200)`
- **reputation recalculate 2N queries → 3 queries**: `compute_reputation()` gets optional `_cert_count`/`_strike_severities` kwargs; `recalculate_all_reputations` pre-loads with GROUP BY
- **sla_breaches indexes** (migration 0047): breach_at, resolved_at, plan, priority — all unindexed columns used in admin queries
- **missing FK indexes** (migration 0048): credit_transactions.user_id, worker_strikes.is_active, worker_certifications.passed
- **49 new unit tests**: test_reputation.py (17 tests — tier thresholds, strike penalties, compute_reputation formula via pre-loaded kwargs) + test_quality.py (32 tests — all 7 _compare_answers task-type branches)
- **Webhook template fix**: `{{nested.key}}` dot-notation now works via key.split('.') traversal; bad JSON after template rendering now logs warning; added `webhook_id` UUID to all payloads for idempotency/tracing
- **Credits.py hardening**: Added structlog logging throughout (was completely unlogged); safe `int()` parsing of Stripe metadata credits (was a crash-on-bad-input); `AnyHttpUrl` validation on checkout success_url/cancel_url; logs for signature failures, duplicate events, missing users

**Test count**: 129 → 178 (49 new tests this session).

## Session 2026-03-26 (continued) — Tests, UX improvements, marketplace sort (commits 34e9b8f–e631720)

**Tests added (60 new, 178 → 238 total):**
- **test_analytics.py** (33 tests): `_percentile()` edge cases/interpolation, completion bucketing, `_fmt_dt`, all 5 analytics endpoint auth guards, export format structure
- **test_workers.py** (12 new tests): `compute_level()` thresholds (L1–L20), max level cap, xp_to_next never negative, LEVEL_NAMES table coverage, TASK_XP_BASE coverage
- **test_disputes.py** (15 tests): `_response_key()` canonical JSON, `check_and_apply_consensus()` for all 4 strategies — any_first no-op, requester_review flags, majority_vote 2/3 win + tie + exact-half, unanimous all-agree + dissenter

**UX improvements:**
- **Worker assignment countdown**: Replaced static "Expires at HH:MM" with live `MM:SS remaining` countdown. Turns red < 5 min. On expiry: disables submit button + shows prominent red banner with re-claim link. Prevents confusing 410 errors.
- **Marketplace sort/filter**: Added `sort_by` API param (reward_desc, newest, default=priority+age). Exposed min_reward filter + sort dropdown in browse UI. Fixed pagination bug — "Next/Prev" was losing `mode=browse` param so clicking Next in browse mode silently switched to feed mode.

## Session 2026-03-26 (continued) — Streak XP multiplier, dispute N+1, bulk op tests (commits 8e3e9bf, ffe0f49)

**Streak XP multiplier system (8e3e9bf):**
- Added `STREAK_MULTIPLIER_TIERS` (3d=1.1×, 7d=1.25×, 14d=1.5×, 30d=2×) and `streak_xp_multiplier()` to worker.py
- Fixed `compute_xp_for_task()` to accept `streak_days` kwarg and apply multiplier
- Fixed `submit_task` to load worker BEFORE computing XP, then passes `streak_days=worker.worker_streak_days`
- Extended `WorkerTaskSubmitResponse` with `streak_multiplier: float` and `streak_days: int` fields
- Rewrote `/worker/submitted.astro` to show streak bonus banner + progressive tier tips
- Updated `/worker/tasks/[id].astro` submit redirect to pass streak params
- Added 18 new tests in test_workers.py for multiplier tiers, monotonicity, and compute_xp_for_task (238 → 256 total)

**Disputes page improvements (ffe0f49):**
- **N+1 SSR fixed**: replaced `for` loop with 2 sequential awaits per task → `Promise.all` fan-out (all tasks + consensus fetched concurrently)
- **12 dark-mode CSS fixes**: `border-gray-100` → `border-gray-800`, `bg-gray-50` → `bg-gray-800`, `text-gray-900` → `text-gray-100` inside `bg-gray-900` cards, vote bars `bg-gray-200` → `bg-gray-700`, timeline spine `bg-gray-200` → `bg-gray-700`
- **Skill-aware empty state on worker dashboard**: detects `profileStatus.missing.includes("skills")` and shows targeted "Add skills →" + "Browse all tasks" CTAs instead of generic "Check back soon"

**32 new bulk operations unit tests (ffe0f49):**
- `test_bulk_operations.py`: bulk_cancel (3 cancellable statuses, 2 non-cancellable, not-owned, mixed batch), bulk_archive (3 terminal, 2 non-terminal, all-terminal batch), bulk_action cancel+retry (success paths, wrong-status to failed list, human task rejected for retry, mixed batch), rerun credit calculation (human formula with 20% fee + floor-1, AI TASK_CREDITS lookup, HUMAN_TASK_BASE_CREDITS coverage)
- **Test count**: 238 → 270 (32 new tests)

## Session 2026-03-26 (continued) — Skills interests, notification reliability, dispute robustness (commits e45e77b, 2cf2131, 9dc8ac9)

**Worker skill interests system (e45e77b):**
- Migration 0049: adds `worker_skill_interests` JSON column to users table
- API: `GET/PATCH /v1/worker/interests` — declare which task types you want to work on
- Enrollment: `BecomeWorkerRequest.skills` now actually saved to `worker_skill_interests`
- Feed seeding: 1.5× `match_weight` boost in `rank_tasks_for_worker` for new workers with no earned proficiency but a declared interest
- Frontend: `/worker/skills` empty state is now an interactive picker (8 task type toggles → save → redirect to marketplace); existing-skills view has compact interest manager panel
- Astro API proxy: `/api/worker/interests.ts` (GET + PATCH)
- 11 new tests (validator, HUMAN_TASK_TYPES_SET, match boost); 288 → 299 total

**Notification reliability (2cf2131):**
- `core/background.py`: new `safe_create_task()` utility — wraps `asyncio.create_task()` with error-logging done callback
- `core/email.py`: `get_running_loop()` (was deprecated `get_event_loop()`); `email.disabled_skipped` INFO (was DEBUG); `email.sent` success log added
- `routers/tasks.py`: 21× `asyncio.create_task()` → `safe_create_task()`
- `routers/worker.py`: 2× `create_task()` calls hardened
- `routers/auth.py`: 3× `asyncio.ensure_future()` → `safe_create_task()`

**Dispute state visibility (9dc8ac9):**
- Dashboard task detail: prominent ⚠️ dispute banner (explains cause, links to /dashboard/disputes), ✅ resolved banner; consensus strategy badges (Majority vote / Unanimous / Manual review) + dispute pill in submissions panel header; "Resolve dispute →" replaces "Review queue →" when disputed
- Worker task detail: detects already-submitted / approved / rejected assignments (4-way parallel fetch, non-fatal); shows outcome panel (⏳ / ✅ / ❌) instead of confusing "Claim & Start"

## Priorities for Next Session 🔜

PHASE: Pre-alpha development. Focus on quality/depth. NOT in scope: launch tasks, marketing, directory listings.

1. **Requester onboarding completion funnel analysis**: The onboarding funnel admin page was built but never audited for actual completion rates. Check the step counts in the DB, identify which steps have the most abandonment, and fix any UX gaps in those steps.
2. **Worker certification system UX audit**: Certifications exist (`/worker/certifications`) but the flow from "no certifications" to "certified" is not smooth — check empty state, quiz UX, and the certification badge on the worker profile.
3. **SSE live updates for task detail**: The task detail page has a "live" indicator and polls via SSE but the SSE endpoint behaviour on human tasks (worker claims, submissions arriving) has not been tested. Verify the SSE feed correctly pushes assignment count updates.

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
- **cs_token is httpOnly** — client-side JS cannot read it via `document.cookie`. Any existing pages that try `document.cookie.match(/cs_token=.../)` will get null — those client-side API calls will fail with 401. Use Astro API route proxies for client-side auth needs.
- **withTimeout(promise, ms, fallback)** in `src/lib/api.ts` — use for non-critical SSR fetches to avoid blocking page render on slow analytics queries.
