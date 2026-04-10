# CrowdSorcerer

**AI-native task broker.** Submit a typed task — web research, entity
lookup, document parsing, LLM generation, transcription, screenshots,
code execution — and CrowdSorcerer routes it to the right worker (AI
service or human marketplace), returns structured results, and bills
per task.

Live at **[crowdsourcerer.rebaselabs.online](https://crowdsourcerer.rebaselabs.online)**.

## Monorepo layout

```
crowdsourcerer/
├── apps/
│   ├── api/          FastAPI task broker (Python 3.10+, async)
│   └── web/          Astro 5 SSR frontend + dashboard
├── packages/
│   ├── sdk/          TypeScript client SDK (@crowdsourcerer/sdk)
│   ├── python-sdk/   Python client SDK (crowdsourcerer)
│   └── types/        Shared TypeScript types (@crowdsourcerer/types)
├── e2e/              Playwright end-to-end specs
├── docs/             Architecture + API docs
└── CLAUDE.md         Engineering rules for agents working in this repo
```

## Runtime requirements

| Layer | Minimum | Production |
|-------|---------|------------|
| Python (API) | **3.10** (match statements, PEP 604 unions) | 3.12 (Docker) |
| Node (web, SDKs) | 20 LTS | 22 LTS |
| pnpm | 9.x | 9.x |
| PostgreSQL | 14 | 16 |

See `apps/api/pyproject.toml` for the declared Python floor and
`package.json` workspace for the Node toolchain.

## Quick start

```bash
# First time only — installs git hooks (pre-commit + pre-push)
make setup-hooks

# Start the full stack (API + Web + Postgres via Docker Compose)
make dev

# Run all tests
make test

# Mandatory before every commit that touches apps/web
make check            # astro check + pnpm build, must be 0 errors
```

## Environment

Copy the relevant `.env.example` inside each app before running:

```
# apps/api/.env
DATABASE_URL=postgresql+asyncpg://crowd:crowd@localhost:5432/crowdsourcerer
REBASEKIT_API_KEY=...
REBASEKIT_BASE_URL=https://api.rebaselabs.online
STRIPE_SECRET_KEY=...
STRIPE_WEBHOOK_SECRET=...
CROWDSOURCERER_API_KEY_SALT=...
JWT_SECRET=...

# apps/web/.env
PUBLIC_API_URL=http://localhost:8100
PUBLIC_SITE_URL=https://crowdsourcerer.rebaselabs.online
```

`PUBLIC_SITE_URL` drives canonical links, Open Graph tags, and the
sitemap — override it for staging, preview, and white-label builds.

## Contributing

Read **[CLAUDE.md](./CLAUDE.md)** first. It contains the hard rules
for engineers and agents touching this repo: strict type safety, no
silent fallbacks, Astro 5 cookie API pitfalls, and the mandatory
`make check` pre-commit gate.

## SDKs

- **TypeScript / Node / Edge / Deno / Browser**
  ```bash
  pnpm add @crowdsourcerer/sdk
  ```
  Supports automatic retries (exponential backoff with jitter),
  `verifyWebhook` (Node crypto) and `verifyWebhookAsync` (Web Crypto).

- **Python**
  ```bash
  pip install crowdsourcerer-sdk
  ```
  Sync and async clients. API keys have the `csk_` prefix.

## License

Proprietary — all rights reserved.
