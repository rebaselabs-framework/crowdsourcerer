"""CrowdSorcerer API — main entrypoint."""
from __future__ import annotations
import time
import uuid

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.openapi.utils import get_openapi
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from core.config import get_settings
from core.database import Base, engine, AsyncSessionLocal
from core.sweeper import start_sweeper, stop_sweeper
from routers import auth, credits, tasks, users, worker, leaderboard, badges, challenges, quality, admin, webhooks, payouts, referrals, notifications, skills, disputes, export, orgs, pipelines, certifications, analytics, marketplace, reputation, triggers, search, experiments, onboarding, sla
from workers.base import get_rebasekit_client

settings = get_settings()
logger = structlog.get_logger()

# ─── Rate limiting ────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

_DESCRIPTION = """
## CrowdSorcerer API

**CrowdSorcerer** is an AI-native task crowdsourcing platform that combines
human intelligence with AI automation to complete any task at scale.

### Key concepts

| Concept | Description |
|---------|-------------|
| **Task** | A unit of work — either run by AI workers or posted for human workers |
| **Credits** | Platform currency. 1 USD = 100 credits |
| **Worker** | A user who completes human tasks and earns credits |
| **Requester** | A user who creates tasks and spends credits |
| **Pipeline** | A sequence of tasks that chain together |
| **Webhook** | HTTP callback fired on task lifecycle events |

### Authentication

All endpoints (except `/health` and `/v1/tasks/public`) require a
`Bearer` token obtained from `POST /v1/auth/token` or an API key set in
`X-API-Key` header.

### Webhook events

Subscribe your task to lifecycle events via the `webhook_events` field:
- `task.created` `task.assigned` `task.submission_received`
- `task.completed` `task.failed` `task.approved` `task.rejected`
- `sla.breach`

Full catalogue: `GET /v1/webhooks/events`

### SDKs

- **Python**: `pip install crowdsourcerer-sdk`
- **TypeScript/JS**: `npm install @crowdsourcerer/sdk`
"""

_TAGS_METADATA = [
    {"name": "auth", "description": "Registration, login, and API key management"},
    {"name": "tasks", "description": "Create and manage AI and human tasks"},
    {"name": "worker", "description": "Worker marketplace — browse, claim, and submit tasks"},
    {"name": "credits", "description": "Credit balance, top-up, and transaction history"},
    {"name": "webhooks", "description": "Webhook delivery logs and event type catalogue"},
    {"name": "pipelines", "description": "Multi-step task pipelines with AI/human chaining"},
    {"name": "certifications", "description": "Worker skill certifications for quality assurance"},
    {"name": "experiments", "description": "A/B testing framework for task configuration"},
    {"name": "sla", "description": "SLA (Service Level Agreement) monitoring and breach alerts"},
    {"name": "leaderboard", "description": "Worker leaderboard by XP, tasks, and earnings"},
    {"name": "notifications", "description": "In-app notification center"},
    {"name": "orgs", "description": "Team and organization management with shared credits"},
    {"name": "analytics", "description": "Requester-facing task cost and completion analytics"},
    {"name": "marketplace", "description": "Community template marketplace for reusable task configs"},
    {"name": "reputation", "description": "Worker reputation scores and moderation strikes"},
    {"name": "payouts", "description": "Worker payout requests and admin review"},
    {"name": "referrals", "description": "Referral/invite system with credit rewards"},
    {"name": "admin", "description": "Platform administration (admin only)"},
    {"name": "search", "description": "Global and advanced search across tasks, pipelines, templates"},
    {"name": "health", "description": "Health check and version info"},
]

app = FastAPI(
    title="CrowdSorcerer API",
    description=_DESCRIPTION,
    version=settings.app_version,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=_TAGS_METADATA,
    contact={
        "name": "RebaseLabs",
        "url": "https://rebaselabs.online",
        "email": "support@rebaselabs.online",
    },
    license_info={
        "name": "MIT",
        "url": "https://opensource.org/licenses/MIT",
    },
    servers=[
        {"url": "https://api.rebaselabs.online", "description": "Production"},
        {"url": "http://localhost:8100", "description": "Local development"},
    ],
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ─── CORS ─────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Request ID + logging middleware ──────────────────────────────────────

@app.middleware("http")
async def request_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    t0 = time.perf_counter()
    request.state.request_id = request_id

    response: Response = await call_next(request)

    duration_ms = round((time.perf_counter() - t0) * 1000)
    response.headers["X-Request-Id"] = request_id
    response.headers["X-Response-Time"] = f"{duration_ms}ms"

    logger.info(
        "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=duration_ms,
        request_id=request_id,
    )
    return response

# ─── Routers ──────────────────────────────────────────────────────────────

app.include_router(auth.router)
app.include_router(tasks.router)
app.include_router(credits.router)
app.include_router(users.router)
app.include_router(worker.router)
app.include_router(leaderboard.router)
app.include_router(badges.router)
app.include_router(challenges.router)
app.include_router(quality.router)
app.include_router(webhooks.router)
app.include_router(admin.router)
app.include_router(payouts.router)
app.include_router(referrals.router)
app.include_router(notifications.router)
app.include_router(skills.router)
app.include_router(disputes.router)
app.include_router(export.router)
app.include_router(orgs.router)
app.include_router(pipelines.router)
app.include_router(certifications.router)
app.include_router(analytics.router)
app.include_router(marketplace.router)
app.include_router(reputation.router)
app.include_router(triggers.router)
app.include_router(search.router)
app.include_router(experiments.router)
app.include_router(onboarding.router)
app.include_router(sla.router)

# ─── Health ───────────────────────────────────────────────────────────────

@app.get("/health", tags=["health"])
async def health():
    return {
        "status": "ok",
        "version": settings.app_version,
        "service": "crowdsourcerer-api",
    }


@app.get("/", tags=["health"])
async def root():
    return {
        "name": "CrowdSorcerer API",
        "version": settings.app_version,
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi_spec": "/openapi.json",
        "sdks": {
            "python": "pip install crowdsourcerer-sdk",
            "typescript": "npm install @crowdsourcerer/sdk",
        },
    }


@app.get("/openapi.json", include_in_schema=False)
async def get_openapi_spec():
    """Download the raw OpenAPI 3.x spec as JSON."""
    return app.openapi()

# ─── Startup / Shutdown ───────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    logger.info("startup", version=settings.app_version)
    # Create tables (dev only — use Alembic in prod)
    if settings.debug:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    # Start background sweeper (expires timed-out task assignments)
    start_sweeper(AsyncSessionLocal)
    logger.info("sweeper.scheduled", interval_seconds=300)


@app.on_event("shutdown")
async def shutdown():
    logger.info("shutdown")
    stop_sweeper()
    await get_rebasekit_client().close()

# ─── Global error handler ─────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled_error", path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "message": "An unexpected error occurred"},
    )
