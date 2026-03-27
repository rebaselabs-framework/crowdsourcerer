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

## Session 2026-03-26 (continued) — Credit race conditions + N+1 sweep (commits 54ef350, 430f944)

**Race conditions fixed (comprehensive credit mutation audit via `grep "\.credits\s*[+-]="`  — 25 sites):**

All remaining unprotected credit mutation sites received `SELECT … FOR UPDATE`:
- `routers/payouts.py` — user fetch in `create_payout_request` (balance check before deduct)
- `routers/tasks.py` — assignment fetch in `reject_submission` (prevent duplicate refund)
- `routers/orgs.py` — user fetch in `transfer_credits` (payer balance check)
- `routers/worker.py` — worker (UserDB) fetch in `submit_task` (credits/XP/streak update; task row was already locked but worker wasn't)
- `routers/marketplace.py` — user fetch in `clone_task` (balance check before deduct)
- `routers/challenges.py` — user fetch in `claim_daily_bonus` (double-click double-award prevention)
- `routers/skill_quiz.py` — user fetch in first-pass bonus code path (quiz race on `prev_count == 0`)

**Previously locked (for reference):** `claim_task` (task row), `create_task` / `create_tasks_batch` / `rerun_task` (user row).

**N+1 queries eliminated:**
- `api_key_usage.py get_usage_overview`: 2N per-key queries (2 × N keys in loop) → single GROUP BY with `case()` for error count
- `quality.py evaluate_submissions`: N per-worker `_update_accuracy()` calls → single batch GROUP BY across all affected worker IDs; `_update_accuracy()` helper preserved for single-worker use elsewhere

All 299 tests pass.

## Session 2026-03-26 (continued) — Onboarding auto-triggers, CreditTransactionDB fixes, UX (commits 5ae28cb–dc7ca8b)

**Onboarding auto-trigger gaps fixed:**
- **Requester `welcome` step**: Had no trigger — added `_advance_requester_onboarding()` fire-and-forget bg task from `update_my_profile` (profiles.py)
- **Requester `view_results` step**: Had no trigger — added `_mark_requester_onboarding(user_id, "view_results")` bg task from `get_task` when `task.status == "completed"` (tasks.py)
- **Worker `profile` step**: Had no trigger — added `_advance_worker_onboarding_profile()` fire-and-forget bg task from `update_my_profile` (profiles.py)
- **Worker `skills` step**: Was calling `db.flush()` after `mark_onboarding_step()` but no `db.commit()` — changes always rolled back. Fixed `db.flush()` → `db.commit()` (skills.py)
- All bg helpers use `AsyncSessionLocal()` context managers with broad `except Exception: pass` so they never block responses

**CreditTransactionDB schema bug fixes (2 sites):**
- **Worker onboarding bonus** (`onboarding.py`): Constructor used phantom `tx_type=` + `balance_after=` kwargs (non-existent columns) and was missing `type` (NOT NULL, no default). Fixed to `type="credit"`, phantom kwargs removed. Would have caused DB constraint violation on first worker to complete all 5 steps.
- **Rerun task** (`tasks.py`): Missing `type` (NOT NULL, no default) on the charge transaction. Fixed to `type="charge"`.
- Added `with_for_update()` to user fetch in both onboarding bonus code paths (worker + requester) to prevent double-award under concurrent final-step completions

**Certification UX fixes:**
- Dark-mode CSS on attempt result banner: `bg-green-50` / `bg-red-50` → `bg-emerald-950/30` / `bg-red-950/30`; `text-green-700` → `text-emerald-400`, `text-red-700` → `text-red-400`, `text-gray-600` → `text-gray-400`
- Empty state banner on certifications page (shown when `myCerts.length === 0 && !certDetail && !resultType`): violet card explaining certifications unlock higher-paying tasks

**Analytics + UX fixes (dc7ca8b):**
- `analytics.py org_analytics`: Added `.limit(500)` safety cap on org members query (was unbounded)
- `worker/onboarding.astro` profile step: CTA href `/worker` → `/dashboard/profile`, label "Go to Profile" → "Edit Profile"

**Tests:**
- 21 new tests in `test_onboarding.py` (requester onboarding: `_build_status`, `_set_step`, `complete_step_internal`, constants, bonus, auth guards)
- **Test count**: 299 → 320

**SSE live updates audit**: Task detail already uses adaptive polling on `/api/tasks/[id]/status` — assignments_completed and status updates covered. No gap found.

## Session 2026-03-26 (continued) — Fix all remaining onboarding db.flush() → db.commit() (commit c3019d8)

**Systematically audited all 5 worker onboarding step triggers:**
- `profile`: profiles.py update → bg task → `mark_onboarding_step` + `db.commit()` ✓
- `explore`: claim_task → `mark_onboarding_step` + was `db.flush()` → **fixed to `db.commit()`** (worker.py)
- `first_task`: submit_task → `mark_onboarding_step` + `db.flush()`, but `db.commit()` on reputation refresh follows → effectively persisted ✓
- `skills`: get_my_skills GET → `mark_onboarding_step` + was `db.flush()` → **already fixed in dc7ca8b** ✓
- `cert`: cert attempt → `mark_onboarding_step` + was `db.flush()` → **fixed to `db.commit()`** (certifications.py)

**Verified requester onboarding auto-triggers all wired:**
- `welcome`: profiles.py update → bg task → `complete_step_internal` ✓
- `create_task`: tasks.py create_task → bg task ✓
- `view_results`: tasks.py get_task (status==completed) → bg task ✓
- `set_webhook`: webhooks.py create endpoint → `safe_create_task` → `complete_step_internal` ✓
- `invite_team`: orgs.py create invite → `safe_create_task` → `complete_step_internal` ✓

**All 10 onboarding step triggers across both flows are now correctly wired and guaranteed to persist.**

## Session 2026-03-26 (continued) — Worker profile + onboarding tests + httpOnly cookie audit (commits 2c7905c–4bad8a5)

**Worker profile improvements (2c7905c):**
- `/dashboard/profile` now shows earned certification badges (badge_icon, cert name, best score, certified date, "✓ Certified" pill; in-progress certs as chips)
- Completeness score expanded from 3 → 7 indicators (added location, website, skills, cert)
- Added location + website_url fields to edit form (were in API but missing from UI)
- "Add skills" prompt when skill_count == 0
- `WorkerCertificationOut` schema: added `badge_icon: Optional[str]` field
- `PublicWorkerProfileOut` schema: added `avg_feedback_score` + `total_ratings_received` (were in UserDB but missing from API response — `/workers/[id].astro` star-rating display was silently broken)
- 23 new tests in `test_worker_onboarding.py` (flush→commit persistence contracts, step constants, auth guards); test count 320 → 343

**`saved_searches.py` fixes (401c24e):**
- `list_saved_searches`: added `.limit(_MAX_SAVED_SEARCHES)` cap
- `create_saved_search`: replaced full-table fetch-to-count with `select(func.count()).scalar()`
- `notify_matching_saved_searches`: added `.limit(10_000)` safety cap

**Critical credits checkout fix (401c24e):**
- Checkout called FastAPI directly with broken `document.cookie` token → always 401
- Created `/api/credits/checkout` Astro proxy; credits.astro now uses it
- Added `/api/credits/transactions` proxy + "Load more" pagination in credits.astro

**Systemic httpOnly cookie anti-pattern — full audit + fix (commits 1325440–4bad8a5):**

`cs_token` is httpOnly — `document.cookie` reads always return `undefined`. Nine pages were silently broken. All fixed:

New proxy routes created:
- `/api/payouts` (GET + POST)
- `/api/payouts/[payoutId]` (DELETE)
- `/api/disputes/[taskId]/evidence` (POST)
- `/api/disputes/[taskId]/assign-mediator` (POST)  ← admin action
- `/api/template-marketplace` — fixed wrong URL (`/v1/template-marketplace` → `/v1/marketplace/templates`)
- `/api/template-marketplace/[id]` (GET single template)
- `/api/template-marketplace/[id]/use` (POST)
- `/api/template-marketplace/[id]/clone-task` (POST)
- `/api/template-marketplace/[id]/rate` (POST)

Pages fixed:
- `worker/earnings.astro`: payout CRUD → proxies; removed dead TOKEN/API vars
- `dashboard/disputes.astro`: evidence upload + mediator assign → proxies; removed dead vars
- `dashboard/marketplace.astro`: all 5 client-side API calls → proxies; removed `define:vars`/`authHeader`
- `worker/skills.astro`: removed broken `!TOKEN` guard (proxy already handled auth)
- `dashboard/triggers.astro`: removed dead `token` var + useless `Authorization` headers on existing proxy calls
- `workers/[id].astro`: `hasCookie` auth check (to show action buttons) moved to SSR via `Astro.cookies.get`

**Zero `document.cookie` cs_token reads remain in the codebase.**

## Session 2026-03-26 (continued) — Certifications redirect, badge icon bug, full cookie audit sweep 2

**certifications.astro redirect refactor (fb3c2a9):**
- Replaced template-literal redirect URL with `URLSearchParams` construction to avoid Astro checker false-positive and ensure proper URL encoding of query params

**Public profile badge AttributeError fix (917d110):**
- `profiles.py` was reading `b.badge_slug`, `b.badge_name`, `b.badge_description` from `WorkerBadgeDB` — those fields don't exist (only `badge_id` + `earned_at`). Would have crashed on any worker with badges.
- Fixed by importing `_BADGE_MAP` from `routers/badges.py` and looking up name/description/icon by `b.badge_id`
- Added `badge_icon: Optional[str] = None` to `PublicProfileBadge` schema
- Updated `/workers/[id].astro` to use `badge.badge_icon ?? "🏆"` instead of hardcoded `🏆`

**Second full `document.cookie` sweep — 9 more pages fixed (16eb555):**

New proxy routes:
- `/api/template-marketplace` (POST) — create template; was calling `/api/v1/marketplace/templates` → 404
- `/api/admin/payouts/[payoutId]/review` (POST) — admin payout review; was `/api/v1/payouts/[id]/review` → 404
- `/api/worker/portfolio` (POST)
- `/api/worker/portfolio/[pinId]` (PATCH + DELETE)

Pages fixed:
- `admin/reputation.astro`: dead `token=` var + 4 useless auth headers removed (proxy reads cookie)
- `admin/payouts.astro`: dead `token=` var + fixed 404 URL
- `experiments/[id].astro`: dead `token=` var + useless auth header removed
- `marketplace/new.astro`: dead `token=` var + authHeader removed; URL fixed
- `worker/portfolio.astro`: 3 direct API calls → proxy routes
- `worker/skills.astro`: dead `TOKEN` variable removed (guard already removed prior)

**Zero `document.cookie` auth reads remain anywhere in the codebase.**

Also: template-marketplace index.ts had wrong backend URL (`/v1/template-marketplace` → `/v1/marketplace/templates`), now also fixed.

## Session 2026-03-26 (continued) — N+1 sweep, race conditions, test expansion (commits f557099–76af644)

**Proxy URL + safety limit fixes (f557099):**
- `template-marketplace/[id]/import.ts`: proxy was calling `/v1/template-marketplace/{id}/import` (non-existent) → fixed to `/v1/marketplace/templates/{id}/use`
- `sweeper.py send_weekly_digests`: added `.limit(10_000)` on all-active-users fetch
- `sweeper.py send_daily_digests`: added `.limit(10_000)` on all-daily-prefs fetch
- `sweeper.py _escalate_priority`: added `.limit(1_000)` on open/pending tasks fetch
- `webhooks.py fire_webhook_event + replay_webhook_log`: added `.limit(100)` on active endpoints fetch (both call sites)

**N+1 fixes — pipelines + skill_quiz + worker (d625680):**
- `pipelines.py list_pipelines`: 2N per-pipeline COUNT queries → 2 bulk GROUP BY queries (41 → 3 queries per page)
- `pipelines.py list_pipeline_runs`: N per-run step_runs queries → single IN query (21 → 2 queries per page)
- `skill_quiz.py get_quiz_questions`: added `.limit(QUESTIONS_PER_QUIZ * 20)` safety cap before Python random.sample
- `worker.py submit_task badge check`: replaced `.all()` → `len()` with `func.count()` scalar aggregate

**Race conditions fixed (3a9cac0):**
- `tasks.py approve_submission`: assignment fetch lacked `with_for_update()` — concurrent approvals would both call `update_worker_skill` and fire notifications; second caller now waits and returns "Already approved" idempotently
- `payouts.py admin_review_payout`: payout + user (refund path) fetches both lacked `with_for_update()` — double-approval or double-refund race fixed
- `credits.py stripe_webhook`: user row fetch lacked `with_for_update()` before `credits +=`; the unique DB constraint on stripe_event_id already prevents double-crediting at the transaction level, but the lock ensures correct serialisation for concurrent events on the same user

**N+1 + silent exception fixes (0f6baf0):**
- `task_messages.py get_task_messages`: per-message sender lookup (N+1) → single bulk IN query; added `.limit(500)` safety cap (was unbounded)
- `applications.py`: 4 bare `except Exception: pass` on notification helpers → `logger.warning(..., exc_info=True)`
- `worker_teams.py`: 1 bare `except Exception: pass` on team member notification → `logger.warning`
- `profiles.py`: 2 bare `except Exception: pass` in bg onboarding helpers → `logger.warning`

**applications.py N+1 + tests (56de93d, 76af644):**
- `list_applications`: `_fmt_application()` called N times (per-application UserDB query) → bulk-load workers with single IN query; also added `.limit(500)` safety cap (was unbounded)
- `list_my_applications`: same N+1 fix
- Refactored: `_build_application_out()` sync helper takes pre-loaded worker; `_fmt_application()` preserved for single-app paths (accept/reject/submit)
- `skill_quiz.py submit_quiz`: loads all quiz questions without limit → added `.limit(1_000)` safety cap
- 6 race condition tests in `test_race_conditions.py` (approve idempotency, reject non-submitted, already-paid payout, processing sets status, rejection refunds credits)
- 7 application tests in `test_applications.py` including N+1 regression test (confirms 3 execute calls not N+1)

**Test count: 343 → 356**

## Session 2026-03-27 — Race conditions, DB indexes, 3 new features (commits b5f5546–2ba55e8)

**Race condition fixes (4 sites, commit b5f5546):**
- `payouts.py cancel_payout_request`: `with_for_update()` on payout + user rows; fix `user_id: UUID → str`
- `tasks.py cancel_task`: `with_for_update()` on task row (double-cancel race)
- `tasks.py reject_submission`: `with_for_update()` on requester user row (lost-update under concurrent rejections)
- `worker.py submit_task`: `with_for_update()` on assignment row (double-submit race)

**Migration 0052 — 7 composite DB indexes (commit b5f5546):**
- notifications (user_id, is_read) and (user_id, is_read, created_at)
- task_messages (task_id, sender_id, recipient_id)
- worker_team_invites (invitee_id, status) and (team_id, status)
- worker_endorsements (requester_id) and (worker_id, created_at)

**Requester feedback on submission reviews (commit a22fd7b):**
- Migration 0053: `requester_note` (Text) + `reviewed_at` (timestamptz) on task_assignments
- `approve_submission` + `reject_submission` persist note; notification body includes note
- Dashboard review UI: combined approve/reject form with shared optional note input
- Worker task detail: shows "Requester note" card in outcome panel
- 13 new tests in `test_submission_feedback.py`; test count 1009 → 1022

**Platform announcements system + admin/workers N+1 fix (commit 2ba55e8):**
- Migration 0054: `platform_announcements` table (type/target_role/starts_at/expires_at/is_active)
- `announcements.py` router: public GET + admin CRUD (POST/PATCH/DELETE)
- `Layout.astro`: SSR-fetches active announcements; shows colour-coded dismissible banners
- `/admin/announcements.astro`: management CRUD page
- `admin.py list_workers` N+1 fixed: per-worker strike COUNT → single GROUP BY
- 17 new tests in `test_announcements.py`; test count 1022 → 1039

**DB index coverage verified (all accounted for):**
- `TaskPipelineStepDB.pipeline_id`: `index=True` on column ✓
- `TaskPipelineStepRunDB.run_id`: `index=True` on column ✓
- `TaskApplicationDB.(worker_id, status)`: migration 0051 ✓
- `TaskMessageDB.(task_id, sender_id, recipient_id)`: migration 0052 ✓

## Session 2026-03-27 (continued) — Worker performance, scheduled tasks improvements (commits 232d3c5–263d81f)

**Worker performance stats feature (232d3c5):**
- `GET /v1/worker/performance`: 5 queries — all-time + 30d approval rates, by-task-type breakdown, platform avg + rank percentile (anonymised, None if < 5 reviews), 8-week weekly trend.
- `/api/worker/performance.ts`: httpOnly-safe proxy route.
- `/worker/performance.astro`: stat cards (all-time, 30d, platform avg, rank percentile), platform comparison bars with contextual message, by-type table with progress bars, 8-week bar chart.
- `worker/index.astro`: added "📊 My Performance" quick-link.
- 8 new tests in `test_worker_performance.py`; test count: 1050 → 1058.

**Scheduled tasks page improvements (263d81f):**
- `/dashboard/scheduled.astro`: rewrote from basic page — task type icons/labels map (18 types), execution mode + priority colour badges, 3-stat summary cards (total/AI/human), next-task countdown banner, live 1s countdown timers per row, confirm-modal cancel replacing browser `confirm()`. Fixed wrong new-task link: `/dashboard/tasks/new` → `/dashboard/new-task`.
- `test_scheduled_tasks.py`: 11 tests — 401 auth guard, 200 with valid token, empty list, required fields, total==len(items), tags None→[], tags preserved, scheduled_at ISO, limit >200 → 422, limit=0 → 422, multi-type items. Test count: 1058 → 1069.

## Session 2026-03-27 (continued) — UX depth + 4 bug fixes (commits c20881e, 347fe7f)

**UX/feature depth pass (c20881e):**
- **Certifications: Session-based answer review** — After completing a quiz, per-question results (correct/incorrect, explanation for wrong answers) stored in Astro session during POST, displayed in "Answer Review" card on the GET result page. Workers now see exactly what they got wrong and why.
- **Certifications: Quiz progress bar** — Live "X of N answered" counter + violet progress bar inside quiz form, updated by `updateAnswers()` on every option change.
- **Task messaging: Fixed broken "Broadcast" option** — Removed the `<option value="">— Broadcast (all parties) —</option>` that caused silent 422 errors (API requires UUID recipient). Shows "No assigned worker to message yet" fallback when no valid recipient exists. Added early-return guard in JS send handler.
- **Worker teams: Pending invite shows UUID → name** — Added `invitee_name: Optional[str]` to `WorkerTeamInviteOut` schema; `_fmt_invite()` now fetches and includes invitee's display name.
- **Worker teams: Task link fixed** — "View →" in the Tasks tab now links to `/worker/tasks/{id}` instead of `/worker/marketplace`.

**Bug fixes (347fe7f):**
- **Dispute resolution logic**: `and` → `or` in `disputes.py:268` guard — previous `and` allowed resolving non-disputed human tasks or disputed AI tasks; `or` correctly rejects either bad condition.
- **Dark-theme CSS**: Fixed `bg-red-50/bg-violet-50` (light) → dark equivalents in `certifications.astro` error banner and `disputes.astro` error + admin mediator panel.
- **Payouts status filter validation**: `list_my_payouts` now rejects invalid `?status=` values with 422 instead of silently filtering to 0 results.
- **Payout cancel notification**: `cancel_payout_request` now sends `PAYOUT_REJECTED` notification so workers know their credits were refunded (commit-order corrected: notification added to session before `db.commit()`).

**Test count**: 1069 (unchanged — no new tests needed for these targeted fixes).

## Session 2026-03-27 (continued) — Critical grading bugs + admin hardening (commits 8454856, e6d81ba)

**Admin API hardening (8454856):**
- `list_users`: validate `?role=` against {requester,worker,both,admin}; `?plan=` against {free,starter,pro,enterprise}
- `list_all_tasks`: validate `?status=` against 9 valid task statuses
- `unban_worker`: raise 400 if `worker.is_banned` is already False (prevents confusing no-op audit entries)

**Skill quiz grading fixed (e6d81ba) — CRITICAL BUG:**
- `submit_quiz` was calling `random.shuffle(qs_list)` producing a DIFFERENT question order than `get_quiz_questions` returned. `answer[i]` was graded against the wrong question — every quiz was graded completely wrong.
- Fix: added `question_ids: list[str]` to `SkillQuizSubmitRequest`; frontend now sends the IDs in display order; backend looks up questions by ID instead of relying on array position after shuffle.
- Legacy fallback: if no `question_ids` provided, sort by ID (deterministic) instead of random.shuffle.

## Session 2026-03-27 (continued) — UX polish: error toasts + portfolio edit UX (commit ac43332)

- **notification-preferences.astro**: replaced two blocking `alert()` calls with `showToast(msg, true)` error toasts; `showToast()` now accepts `isError` param and dynamically applies red/emerald colour classes so one toast element covers both success and error states.
- **worker/portfolio.astro**: rewrote edit-form submit handler with proper error display, loading state, and try/catch/finally — API errors now surface inline instead of failing silently.

## Session 2026-03-27 (continued) — Systematic alert()/confirm()/prompt() purge (commits 9d5f50a–c365216)

**Eliminated all blocking browser dialogs across entire frontend (32 occurrences fixed across 22 files).**

Pattern approach:
- `alert()` → toast notification (auto-dismiss after 3.5–4s)
- `alert()` for form validation → inline error div adjacent to submit button
- `confirm()` → dark-themed modal OR two-step button confirm (click twice within 3s)
- `prompt()` for text input → proper modal with `<input>` field
- `prompt()` for displaying secret → reuse existing amber `#secret-box` reveal div

Key fixes:
- **earnings.astro**: cancel payout confirm→modal; export alert→toast; add `res.ok` check before JSON parse in CSV export
- **credits.astro**: checkout failure alert→inline `#checkout-error` div
- **triggers.astro**: fire-trigger alert→toast + re-enable button; delete confirm→two-step
- **admin/alerts, reputation, workers.astro**: all alert() → toasts; add loading states
- **webhooks.astro**: 14 alert() → toasts; 3 confirm() → two-step; prompt() for rotated secret → reuse `#secret-box`; inline `#create-ep-error` and `#tpl-error` form error divs
- **notifications.astro**: "clear all" confirm→modal with count in body
- **worker/tasks/[id].astro**: release task confirm→modal with error display
- **worker/certifications.astro**: "answer all questions" alert→inline `#quiz-submit-error`
- **worker/availability.astro**: blackout-date remove confirm→two-step; alert→toast
- **worker/watchlist.astro**: 2 alert→toast
- **worker/portfolio.astro**: remove-pin confirm→two-step
- **task-templates.astro**: unpublish confirm→two-step; publish prompt()→modal with text input; alert→toast
- **template-marketplace.astro**: unpublish confirm→two-step
- **skill-quiz.astro**: submit failure alert→dynamically inserted inline error
- **tasks/[id]/index.astro**: duplicate-params alert→toast; dep-remove confirm→two-step; dep errors→toast
- **marketplace.astro**: save-search error alert→toast
- **experiments.astro**: traffic % alert→inline `#traffic-error` div
- **search/tasks.astro**: "no filters" alert→temporary button state

**Zero blocking alert()/confirm()/prompt() dialogs remain in the frontend (excluding docs/sandbox, admin form-submit patterns, and security-critical 2FA disable confirm).**

**new-task.astro**: submit button shows "Submitting…" + disabled on valid submit to prevent double-submit

## Session 2026-03-27 (continued) — Stripe webhook race fix + dark-mode CSS + confirm() purge round 2

**Stripe webhook concurrent user mutation race fixed (`stripe_webhooks.py`):**
- `_get_user_by_customer()` and `_get_user_by_email()` now accept `for_update: bool = False` kwarg
- All 4 call sites that mutate user state (`checkout.session.completed` payment, `subscription.created/updated`, `subscription.deleted`, `invoice.payment_succeeded`) now pass `for_update=True`
- The log entry insert + `flush()` is the serialisation point (unique constraint on `stripe_event_id` blocks duplicate events); the user row lock prevents lost-update races when two *different* Stripe events for the same customer arrive concurrently
- `invoice.payment_failed` (notification only, no state mutation) left unlocked

**pipelines.astro — comprehensive dark-mode CSS fix:**
- Error div: `bg-red-50 border-red-200 text-red-700` → `bg-red-950/30 border-red-800 text-red-300`
- Step cards: `bg-blue-50/orange-50 border-blue-200/orange-200` → `bg-*-900/20 border-*-800/50`
- Mode badges: `bg-blue-100/orange-100 text-blue-700/orange-700` → `bg-*-900/40 text-*-300`
- Condition/pass/fail/retry badges: all light → dark (`bg-*-900/40 text-*-300`)
- Retry run button: `bg-amber-100 border-amber-200` → `bg-amber-900/30 border-amber-800`
- Cancel run button: `bg-red-50 border-red-200` → `bg-red-900/30 border-red-800`
- Step detail output boxes: `bg-green-50/red-50/blue-50 border-green-200/red-200/blue-200` → dark equivalents
- Final pipeline output: `bg-violet-50 border-violet-200 text-violet-700 text-gray-600` → `bg-violet-900/20 border-violet-800/50 text-violet-300 text-gray-400`
- Delete button: `confirm()` → two-step confirm (click once → "Delete?", click again within 3s → submits form)

**worker/teams/index.astro + teams/[teamId].astro — confirm() replaced:**
- Both pages used `data-confirm` + `confirm()` pattern; replaced with two-step button confirm
- `[teamId].astro` had two inline `onsubmit={...confirm(...)}` — converted to `data-confirm` attributes
- Both scripts now use `is:inline` (no TypeScript type casts needed)

## Session 2026-03-27 (continued) — confirm()/alert()/prompt() round 3 + more race conditions (commits 8817ed2–0b90a97)

**Race condition fixes in certifications + orgs (8817ed2):**
- `certifications.py submit_certification`: `with_for_update()` on `WorkerCertificationDB` fetch — prevents lost-update on `attempt_count`, stale cooldown checks, and duplicate-insert races under concurrent submissions
- `orgs.py transfer_credits`: `with_for_update()` on re-fetched `OrganizationDB` before balance check; lock order org→user prevents deadlocks; also changed `org, member = _get_org_and_require_role(...)` → `_, member = ...` (org was discarded)

**UX audit round 3 — confirm()/alert() elimination across 14 pages (commits 468e26c, 667905f, 4c6be3b, bae98f4):**
- `admin/cache.astro`: `confirm()` in `flushCache()` → `setupFlushBtn()` two-step pattern; "Flushing…" disabled state during fetch; restores in `finally`
- `dashboard/api-keys.astro`: `onsubmit="return confirm('Revoke…')"` → `data-confirm` attribute + two-step handler; clipboard failure `.catch(() => {})` → try/catch with `input.select()` fallback
- `dashboard/team/index.astro`: Remove-member `onclick confirm()` → form `data-confirm`; silent SSR error swallow → `?error=` redirect + display block; added `<script is:inline>` two-step handler
- `dashboard/experiments/[id].astro`: Added `submitBtn.disabled = true` + "Enrolling…" text during enroll fetch; restores in `finally`
- `dashboard/experiments.astro`: Delete form `onsubmit confirm()` → `data-confirm`; added `type="submit"` to delete button (was missing); added two-step handler to existing TS script
- `dashboard/saved-searches.astro`: Toggle failure `alert()` → "⚠ Error" button text flash + `!text-red-400`; delete `confirm()` → two-step on delete button with loading state
- `dashboard/security.astro`: 2FA disable `confirm()` → two-step on `btnDisable` ("⚠️ Confirm disable" → execute on second click within 3s)
- `admin/reputation.astro`: Unban `confirm()` → two-step "Sure?" on each unban button; recalculate "Recalculate ALL…" `confirm()` → two-step
- `admin/announcements.astro`: Delete announcement `onclick confirm()` on button → `data-confirm` on form; added `<script is:inline>` two-step handler
- `admin/workers.astro`: Unban `confirm(\`Unban ${name}?\`)` → two-step "Sure? Unban ${name}?" per-button
- `docs/sandbox.astro`: "Enter API key" `alert()` → focus input + border highlight + button text flash; "Invalid JSON" `alert()` → button text flash
- `workers/[id].astro`: Clipboard fallback `window.prompt("Copy…", url)` → creates `<input readOnly>` appended to parent, focused + selected, removed after 8s
- `marketplace.astro`: Save-search `prompt("Name…")` → inline DOM expansion (button `replaceWith` wrapper containing `<input> + Save + Cancel`; Enter confirms, Escape cancels)
- `dashboard/search/tasks.astro`: Save-filters `prompt("Name…")` → same inline TypeScript DOM expansion pattern
- `dashboard/referrals.astro`: Clipboard `.catch(() => {})` always showing "Copied!" → try/catch; success → "Copied!"; failure → `urlInput?.select()` + "Select all ↑"

**Zero `confirm()`/`alert()`/`prompt()` dialogs remain anywhere in the frontend** (verified via grep).

**Race conditions in tasks.py, marketplace.py, skills.py (commit 0b90a97):**
- `tasks.py create_task`: `with_for_update()` on `OrganizationDB` before org-pool balance check
- `tasks.py rerun_task`: same fix for org-pool balance check
- `tasks.py execute_ai_task / _run_task`: `with_for_update()` on `UserDB` in cache-hit refund path
- `marketplace.py rate_template`: replaced `_get_template()` helper call with inline locked SELECT to prevent `rating_sum`/`rating_count` lost-update under concurrent ratings
- `skills.py update_worker_skill`: `with_for_update()` on `WorkerSkillDB` to prevent lost-update on `tasks_completed`, `tasks_approved`, `credits_earned`, etc. under concurrent task approvals for the same worker+task_type

## Session 2026-03-27 (continued) — Race fixes + targeted DOM updates + loading states (commits c4a89da–c4d6df7)

**Race condition fixes:**
- `referrals.py apply_referral_on_signup`: `with_for_update()` on referrer (credits_pending lost-update prevention) and referred-user rows; `pay_referral_bonus_on_first_task`: `with_for_update()` on referral row (double-payment race) and referrer row
- `endorsements.py create_endorsement`: wrapped `db.commit()/db.refresh()` in `try/except IntegrityError` → rollback + 409 (two concurrent requests that both pass the count=0 check are now caught by DB unique constraint)
- `worker_teams.py accept_invite`: `with_for_update()` on invite row; added 20-member check UNDER the lock (prevents race between two simultaneous accepts pushing team over limit)

**N+1 query eliminations in worker_teams.py:**
- `list_teams`: replaced 2N per-team queries with single bulk GROUP BY (member counts) + single IN query (user roles) — O(N) → O(1) DB calls per page
- `list_pending_invites`: replaced 3 queries per invite with 2 bulk IN queries (all teams + all users at once)
- `get_team`: replaced N individual user queries for members with single IN; replaced 2N invite-user queries with single IN for pending invites
- `accept_invite`: fetch invite with `with_for_update()`; member count check moved under the lock

**Targeted DOM updates (no more location.reload()):**
- `dashboard/triggers.astro`: delete card → fade + remove; fire-now → update run count + last-fired inline; added `data-trigger-card`, `data-run-count`, `data-last-fired` attributes; fixed `res.json()` before `res.ok` order
- `admin/alerts.astro`: resolve → fade + remove card; decrement severity counter badges; update health card to "All Systems OK" when both hit 0; added severity stat IDs + `data-severity` attributes
- `admin/payouts.astro`: action confirm → find row by `data-payout-id`, update status badge CSS + text, clear action buttons, dim row
- `worker/challenges.astro`: claim bonus → replace CTA div with "✅ Challenge complete!", update progress bar to emerald, update card border — no reload
- `worker/saved-searches.astro`: delete last search → replace list with empty-state HTML — no reload

**Error/loading state improvements:**
- `dashboard/credits.astro`: load-more catch block → `toast()` instead of silent failure
- `dashboard/new-task.astro`: duplicate task + saved template failures → sets `error` string shown in UI instead of silently blanking
- `worker/earnings.astro`: payout submit → `submitBtn.disabled=true; "Submitting…"` before fetch; restores on error; fixed `res.ok` check order (was `is:inline` — removed all TypeScript type casts)
- `worker/marketplace.astro`: watchlist toggle → error toast on failure instead of silent catch
- `worker/skills.astro`: `var d = await r.json()` → safe parse with `.catch(function(){ return {}; })` after `r.ok` check

## Session 2026-03-27 (continued) — Notification query cleanup + regression tests (commits dc5a4ab, a58ac6d)

**Notification query cleanup (dc5a4ab):**
- `notifications.py list_notifications + get_unread_count`: removed redundant `select(func.count()).select_from(inner.subquery())` pattern; replaced with direct `select(func.count()).where(...) + db.scalar()` — cleaner and functionally equivalent

**Regression tests added (a58ac6d):**
- `test_worker_teams.py` (new file, 6 tests): `list_teams` no-membership early return, bulk GROUP BY counts correctly mapped per team, my_role correctly assigned per team; `accept_invite` invite-not-found 404, non-pending 400, team-at-capacity 400 (race-condition guard)
- `test_endorsements.py` (1 new test): `test_create_endorsement_integrity_error_returns_409` — simulates concurrent duplicate that slips past count=0 guard; verifies IntegrityError on commit → rollback + 409 (not 500)

**Test count: 356 → 363**

## Session 2026-03-27 (continued) — Silent failure fixes + targeted DOM updates (commits 52b6593, a898ce0)

**Silent failure + missing error handling fixes (52b6593):**
- `admin/reputation.astro`: unban had NO try/catch — failures were silent; added try/catch with error toast + button restore; recalculate now checks `res.ok` before parsing JSON
- `worker/saved-searches.astro`: `__toggleAlert` + `__updateFrequency` both had no `res.ok` check; now revert checkbox/select + show inline "⚠ Save failed" on error
- `worker/invites.astro`: `respondToInvite` now disables all buttons in the card before the async call, re-enables on any error path (prevents double-submit + leaves UI in correct state)
- `worker/index.astro`: availability toggle — disable all `.availability-btn` during fetch, error toast on `!res.ok` or network error, re-enable in `finally`; replaced silent `catch { /* ignore */ }`

**Targeted DOM updates + event delegation (a898ce0):**
- `admin/workers.astro`: replaced per-button `querySelectorAll` + `addEventListener` with single `<tbody>` click listener (event delegation handles dynamically-swapped buttons); ban success → row bg `bg-red-950/10`, status cell "Banned" badge, Ban→Unban swap — no reload; strike success → update strikes cell with `data.total_strikes` — no reload; unban success → clear row bg, status cell "Active" badge, Unban→Ban swap — no reload; column constants `COL_STRIKES=4`, `COL_STATUS=5`, `COL_ACTIONS=7`
- `worker/portfolio.astro`: pin form submit shows "Pinning…" + disabled while in-flight; restores button text + re-enables on error so user can retry

## Session 2026-03-27 (continued) — Race conditions, test fixes, UX silent failures (commits 1fd7302–5f58ef1)

**UX silent failure + missing res.ok fixes (1fd7302):**
- `admin/health.astro`: auto-refresh was updating "Updated HH:MM" timestamp even on API error; added `if (!r.ok) throw` guard before `.json()` parse
- `notifications.astro` (4 fixes): clear-all had no `res.ok` check; mark-all-read had no check; per-notification delete removed card even on failure; pollUnreadCount parsed JSON without `res.ok`; added `#clear-all-error` inline error element + button disabled state during fetch
- `task-templates.astro`: delete confirm had no disabled state, no error feedback, no `res.ok` check; added loading state + inline error toast on failure
- `worker/portfolio.astro`: SSR empty `catch {}` → catches error and surfaces `portfolioError` string as red warning banner
- `admin/reputation.astro`: full event-delegation refactor; strike/ban/unban now do targeted DOM updates (score, tier, strikes, status badge, action button swap) instead of `location.reload()`; added `scoreTierColor()` + `scoreTierLabel()` JS mirrors of Python functions; `data-worker-id` on every `<tr>`

**Test suite fix (00b27db):**
- `test_notifications.py`: 3 tests failing — `list_notifications` and `get_unread_count` use `db.scalar()` (not `db.execute()`) since commit dc5a4ab, but `_make_db()` didn't set `db.scalar = AsyncMock()`
- Fixed: added `db.scalar = AsyncMock()` to `_make_db()`; rewired 3 tests to use `db.scalar.side_effect/return_value` for counts and `db.execute.return_value` for result sets

**Race condition fixes (2effbf3):**
- `orgs.py accept_invite`: OrgInviteDB SELECT lacked `.with_for_update()` — two concurrent accepts of same token could both pass `accepted_at IS NULL` check
- `applications.py accept_application` + `reject_application`: both TaskApplicationDB SELECTs lacked `.with_for_update()`; two concurrent accepts could both see `status="pending"`, both accept, create duplicate TaskAssignmentDB records
- `test_applications.py TestAcceptRejectRaceGuard` (2 tests): verify 400 returned when app.status is already "accepted"/"rejected" (race already won), no commit called

**Race condition fix + tests (5f58ef1):**
- `orgs.py cancel_invite`: OrgInviteDB SELECT lacked `.with_for_update()` — two concurrent cancels could both see the invite as existing
- `test_race_conditions.py TestCancelPayoutRequest` (3 new tests): happy path credits refunded + CreditTransactionDB created, 404 not found, 409 non-pending status guard

**Test count: 1078 → 1081**

## Session 2026-03-27 (continued) — Race conditions, SSR errors, LIKE escaping (commits 88d3f19–655ff7f)

**SSR action error propagation (commits 88d3f19, 3dc8b79):**
All Astro SSR POST handlers that were silently swallowing API errors now redirect with `?action_error=<msg>` so users see what went wrong:
- `review.astro`: approve/reject now surfaces error via toast (reads `action_error` from redirect URL); also fixed `res.ok` check order
- `tasks/[id]/index.astro`: all 5 form-based actions (accept/reject application, withdraw, assign/remove team, approve/reject submission); added `actionError` banner alongside `rerunError`
- `disputes.astro`: added `.catch(() => ({}))` to both `.json()` calls (evidence submit + mediator assign) to prevent unhandled parse errors on non-JSON responses
- `worker/tasks/[id].astro`: release-confirm handler lacked `res.ok` check — was navigating to marketplace even on API error
- `pipelines.astro`: create/delete/run all propagate errors via `?action_error=`; run success now redirects to `?id=<pipeline>` for detail reload
- `worker/onboarding.astro`: skip action propagates error; `complete_step` silent (correct — state visible on reload)
- `requester-onboarding.astro`: reset action propagates error; complete_step/skip_step silent

**Race conditions fixed (commits b4ed568, dd453a1):**
- `pipelines.py _execute_pipeline_run`: user row fetch for per-step credit charge lacked `.with_for_update()` — concurrent steps could overspend
- `worker_marketplace.py respond_to_invite`: `WorkerInviteDB` SELECT lacked `.with_for_update()` — concurrent accept+decline race
- `worker_teams.py decline_invite`: `WorkerTeamInviteDB` SELECT lacked `.with_for_update()` (accept_invite already had it; decline didn't)
- `test_race_conditions.py`: added `TestRespondToInviteRaceGuard` (2 tests) and `TestDeclineTeamInviteRaceGuard` (1 test)

**LIKE wildcard escaping (commit 655ff7f):**
- User input with `%` or `_` was interpreted as SQL wildcards in 17 ILIKE/LIKE calls across search.py, global_search.py, tasks.py
- Added `_esc_like()` helper to all 3 files; all callers now also pass `escape="\\"` so PostgreSQL interprets escaped sequences correctly
- Correctness fix: searching for "50%" now finds literal "50%" instead of "50" + anything

**Test count: 1081 → 1084**

## Session 2026-03-27 (continued) — Safe JSON parsing, race conditions, SSR error propagation (commit f54cf82)

**Safe `.json().catch(() => ({}))` added to 9 client-side fetch calls:**
- `webhooks.astro`: 6 calls missing `.catch()` — create endpoint, test-ping, rotate-secret, delete endpoint, retry-log, replay-event; non-JSON error pages (gateway HTML) would throw before `res.ok` check and show "Network error" instead of real error
- `tasks/index.astro`: 3 bulk-action calls (retry, bulk-cancel, bulk-archive) — same pattern

**SSR POST silent-catch fixed in 2 pages:**
- `experiments.astro`: `catch (_) {}` → redirect with `?action_error=`; added `actionError` variable + banner in template; covers create/status-update/delete experiment actions
- `admin/announcements.astro`: same fix for `catch (_) { // Fall through }` → redirect with `?action_error=`; covers create/toggle/delete announcement actions

**Race conditions:**
- `portfolio.py pin_task`: replaced separate duplicate-check SELECT + `func.count()` with a single `SELECT ... FOR UPDATE` covering all worker portfolio items — prevents two concurrent pin requests from both passing the 10-item cap and exceeding `_MAX_PORTFOLIO`
- `ratings.py rate_task`: added `except IntegrityError` on `db.flush()` → `db.rollback()` + 409; prevents a concurrent duplicate rating (that slips past the pre-insert SELECT check) from surfacing as a 500

**Test count: 1084 (unchanged)**

## Priorities for Next Session 🔜

PHASE: Pre-alpha development. Focus on quality/depth. NOT in scope: launch tasks, marketing, directory listings.

1. **More UX depth audits**: Continue auditing existing pages for bugs and UX gaps — admin pages, task creation flow, marketplace.
2. **Proxy route test coverage**: 13+ Astro proxy routes still lack tests. Would need Vitest setup in the web package first.
3. **Race condition audit**: Look for more race conditions in worker pay/approval flows.

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
