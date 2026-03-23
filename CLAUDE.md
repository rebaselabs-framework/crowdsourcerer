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
make deploy       # push + trigger Coolify deploy
```

## Conventions

- All Python code: snake_case, type-annotated, async-first
- FastAPI routers in `apps/api/routers/`
- Pydantic models in `apps/api/models/`
- RebaseKit worker clients in `apps/api/workers/`
- Frontend pages in `apps/web/src/pages/`
- Follow RebaseKit patterns for auth, error handling, Dockerfiles
