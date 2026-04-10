# CrowdSorcerer

AI-native crowdsourcing / task-brokering platform.
Submit any task. We route it to the right AI worker and deliver results.

## What it is

CrowdSorcerer is a **task broker** that sits on top of RebaseKit's 20-API suite.
Users don't call individual APIs — they submit high-level tasks, and CrowdSorcerer:

1. Classifies the task (web research, doc parse, LLM generation, transcription, etc.)
2. Routes it to the correct RebaseKit API worker
3. Returns structured results
4. Bills the user per-task (Stripe fiat or crypto)

This is a **separate monorepo** from:
- `rebaselabs-framework/rebasekit` — the underlying API platform (the workers)
- `rebaselabs-framework/rebaselabs-framework` — the agent orchestration infra (runs the Crowder agent)

## Monorepo Layout

```
crowdsourcerer/
├── apps/
│   ├── api/          # FastAPI task broker (the core platform)
│   └── web/          # Astro 5 SSR frontend (task UI + dashboard)
├── packages/
│   ├── sdk/          # TypeScript client SDK (@crowdsourcerer/sdk)
│   └── types/        # Shared TypeScript types (@crowdsourcerer/types)
├── docs/             # Architecture + API docs
├── docker-compose.yml
├── Makefile
└── CLAUDE.md
```

## Tech Stack

| Layer       | Tech                      |
|-------------|---------------------------|
| API backend | FastAPI, asyncpg, pydantic |
| Frontend    | Astro 5 SSR, Tailwind      |
| Database    | PostgreSQL (shared with RebaseKit infra) |
| Queue       | In-process asyncio (v1), upgrade to Redis later |
| Payments    | Stripe (fiat) + crypto rails (BTC/SOL/EVM) |
| Deployment  | Docker Compose on Coolify (Hetzner ARM) |

## RebaseKit Integration

CrowdSorcerer calls RebaseKit APIs as its worker pool:

| Task Type          | RebaseKit API         | Route                          |
|--------------------|-----------------------|--------------------------------|
| `web_research`     | webtask-api           | POST /task                     |
| `entity_lookup`    | entity-enrichment-api | POST /enrich                   |
| `document_parse`   | doc-parse-api         | POST /parse                    |
| `data_transform`   | data-transform-api    | POST /transform                |
| `llm_generate`     | llm-router-api        | POST /v1/chat/completions      |
| `screenshot`       | screenshot-api        | POST /screenshot               |
| `audio_transcribe` | audio-to-text-api     | POST /transcribe               |
| `pii_detect`       | pii-api               | POST /detect                   |
| `code_execute`     | code-exec-api         | POST /execute                  |
| `web_intel`        | web-intel-api         | POST /intel                    |

## Env Variables

```
# apps/api
DATABASE_URL=postgresql+asyncpg://...
REBASEKIT_API_KEY=...
REBASEKIT_BASE_URL=https://api.rebaselabs.online
STRIPE_SECRET_KEY=...
STRIPE_WEBHOOK_SECRET=...
CROWDSOURCERER_API_KEY_SALT=...
JWT_SECRET=...

# apps/web
PUBLIC_API_URL=https://crowdsourcerer.rebaselabs.online
```

## Domain

`crowdsourcerer.rebaselabs.online` (or `crowd.rebaselabs.online`)

## Commands

```bash
make dev          # start all services locally
make test         # run all tests
make check        # MANDATORY before commit/deploy — astro check + build
make deploy       # runs check first, then push + trigger Coolify deploy
make setup-hooks  # install git hooks (pre-commit + pre-push) — run once after clone
```

## 🚨 MANDATORY BUILD CHECKS — NEVER SKIP 🚨

**Every session that touches `apps/web/` MUST run this before committing:**

```bash
cd /workspace/projects/crowdsourcerer
make check
```

This runs:
1. `cd apps/web && pnpm exec astro check` — must report **0 errors**
2. `cd apps/web && pnpm build` — must end with **"Complete!"**

**If either step fails → fix ALL errors → re-run `make check` → only then commit.**

Do NOT use `git commit --no-verify`. Do NOT skip the pre-commit hook. Ever.

### Three Layers of Protection

| Layer | When | What runs |
|-------|------|-----------|
| `make check` | Manually, before every commit | `astro check` + `pnpm build` |
| `.git/hooks/pre-commit` | Auto on `git commit` | `astro check` + `pnpm build` |
| `.git/hooks/pre-push` | Auto on `git push` | `astro check` + `pnpm build` |
| GitHub Actions `web-build` | Auto on every push | `astro check` + `pnpm build` |

All four must pass. GitHub Actions is the canonical CI gate.

### If You're Not Sure — Check First

Before committing **any** change to an Astro file, always:
```bash
cd apps/web && pnpm exec astro check 2>&1 | tail -5
```
Should show: `0 errors`. If not, stop and fix before continuing.

## Astro 5 Coding Rules (STRICT — violations break production)

### 1. Cookie Access — NEVER use `Astro.cookies.getAll()`
`getAll()` does NOT exist in Astro 5's `AstroCookies` API. It will crash at runtime.

```typescript
// ❌ WRONG — throws TypeError at runtime
const token = getToken(Object.fromEntries(Astro.cookies.getAll().map((c) => [c.name, c.value])));

// ✅ CORRECT — use the typed helper
const token = getToken(Astro.cookies);  // getToken accepts AstroCookies directly
```

### 2. TypeScript in `<script>` Tags
Only use TypeScript syntax (`as X`, `!.`, type annotations) in `<script lang="ts">` tags.
Plain `<script>` and `<script define:vars>` tags are JavaScript-only.

```html
<!-- ❌ WRONG — TS syntax in plain script -->
<script define:vars={{ foo }}>
  const el = document.getElementById("x") as HTMLInputElement;
</script>

<!-- ✅ CORRECT — use @ts-ignore or JSDoc, or move to lang="ts" -->
<script define:vars={{ foo }}>
  // @ts-ignore — el is always an input element
  const el = document.getElementById("x");
</script>
```

### 3. `apiFetch` Type Parameters
Always add `<any>` type parameter to `apiFetch` calls in frontmatter if assigning to typed variables.

```typescript
// ❌ WRONG — causes 'unknown not assignable to any[]' errors
let items: any[] = [];
[items] = await Promise.all([apiFetch("/v1/items", { token })]);

// ✅ CORRECT
let items: any = [];
[items] = await Promise.all([apiFetch<any>("/v1/items", { token })]);
```

### 4. Raw Braces in Template Literals
Escape `{` and `}` inside Astro template expressions when they're literal characters.

```html
<!-- ❌ WRONG — { in code example confuses Astro compiler -->
<code>{"{"} "key": "value" {"}"}</code>

<!-- ✅ CORRECT — use HTML entities or template literals -->
<code>&lbrace; "key": "value" &rbrace;</code>
```

## Auth Architecture

- **Access tokens**: JWT, 30-minute expiry, embed `token_version` claim
- **Refresh tokens**: `csrt_` prefix + 64 random chars, stored as SHA-256 hash, 30-day expiry
- **Family-based replay detection**: reusing a revoked refresh token kills the entire family
- **2FA**: TOTP with pyotp, 8 backup codes, rate-limited (5/min)
- **Password change**: increments `token_version` + revokes all refresh tokens
- **Frontend cookies**: `cs_token` (30min httpOnly) + `cs_refresh` (30 days httpOnly)
- **Transparent refresh**: `requireAuth()` in auth.ts auto-refreshes on expired access token

## Security Patterns

- **SSRF protection**: `core/url_validation.py` blocks private/loopback/metadata IPs in webhook URLs
- **Webhook signatures**: `t=TIMESTAMP,v1=HMAC-SHA256` format with `X-Crowdsourcerer-Timestamp` header
- **DB constraints**: CHECK constraints on `credits >= 0` (users + orgs)
- **Rate limiting**: slowapi on sensitive endpoints (auth, 2FA, webhook retry/replay)
- **Template rendering**: Never return raw rendered templates in errors — safe error indicators only

## Conventions

- All Python code: snake_case, type-annotated, async-first
- FastAPI routers in `apps/api/routers/`
- Pydantic models in `apps/api/models/`
- RebaseKit worker clients in `apps/api/workers/`
- Frontend pages in `apps/web/src/pages/`
- Alembic migrations in `apps/api/alembic/versions/`
- Follow RebaseKit patterns for auth, error handling, Dockerfiles
- Always run `make check` before commit (astro check + build)

## Current Status (2026-04-09)

- **Tests**: 2173 backend (0 failures) + 189 E2E Playwright
- **Endpoints**: 335 | **Pages**: 118 | **Migrations**: 67
- **Revenue**: $0 | **Users**: 0
- **Deployment**: Live at crowdsourcerer.rebaselabs.online
- **Phase**: IMPROVE & TEST — quality first, owner decides launch timing
- **Quick health check**: `bash scripts/healthcheck.sh` (12 checks in <15s)

## Deployment Troubleshooting

The app is docker-compose based (API + Web + PostgreSQL). Common issues:

1. **Container restart loop, no logs**: Build probably failed. Try `agent deploy redeploy <id>`.
2. **503 on domain**: App not running. Check `agent deploy status <id>`.
3. **Migration failure**: API container depends on PostgreSQL health check. If DB isn't ready, migrations fail and container exits.
4. **Web build failure**: Astro build requires all packages built first (types → sdk → web). If pnpm-lock.yaml is stale, frozen-lockfile fails.
5. **ARM architecture**: Hetzner VPS is ARM. All base images must support arm64.
