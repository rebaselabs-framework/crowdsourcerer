"""CrowdSorcerer API — main entrypoint."""
from __future__ import annotations
import time
import uuid
from contextlib import asynccontextmanager

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
from core.webhook_retry import start_retry_worker, stop_retry_worker
from routers import auth, credits, tasks, users, worker, leaderboard, badges, challenges, quality, admin, webhooks, payouts, referrals, notifications, skills, disputes, export, orgs, pipelines, certifications, analytics, marketplace, reputation, triggers, search, experiments, onboarding, sla, comments, stripe_webhooks, profiles, two_factor, saved_searches, api_key_usage, skill_quiz, requester_onboarding, webhook_templates, task_dependencies, endorsements, worker_marketplace, ratings, portfolio, requester_templates, worker_teams, applications, availability, task_messages, notification_digest, global_search, oauth, platform_stats, announcements, leagues, quests
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

@asynccontextmanager
async def lifespan(application: FastAPI):
    """Application startup and shutdown lifecycle manager."""
    # ── Startup ───────────────────────────────────────────────────────────
    logger.info("startup", version=settings.app_version)

    # Reject insecure default secrets in non-debug mode
    if not settings.debug:
        _PLACEHOLDER = "change-me-in-production"
        if settings.jwt_secret == _PLACEHOLDER:
            raise RuntimeError(
                "FATAL: JWT_SECRET is set to the insecure default. "
                "Set a strong random value via the JWT_SECRET environment variable."
            )
        if settings.api_key_salt == _PLACEHOLDER:
            raise RuntimeError(
                "FATAL: API_KEY_SALT is set to the insecure default. "
                "Set a strong random value via the API_KEY_SALT environment variable."
            )

    # Create tables (dev only — use Alembic in prod)
    if settings.debug:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    # Start background sweeper (expires timed-out task assignments)
    start_sweeper(AsyncSessionLocal)
    logger.info("sweeper.scheduled", interval_seconds=300)

    # Start persistent webhook retry worker (polls every 30s)
    start_retry_worker()
    logger.info("webhook_retry_worker.scheduled", interval_seconds=30)

    yield  # Application runs here

    # ── Shutdown ──────────────────────────────────────────────────────────
    logger.info("shutdown")
    stop_retry_worker()
    stop_sweeper()
    await get_rebasekit_client().close()


app = FastAPI(
    title="CrowdSorcerer API",
    description=_DESCRIPTION,
    version=settings.app_version,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=_TAGS_METADATA,
    lifespan=lifespan,
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
    from core.system_alerts import record_http_error  # local import to avoid circular  # noqa: PLC0415
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

    # Track 5xx errors for system alert monitoring
    if response.status_code >= 500:
        record_http_error(path=request.url.path, status_code=response.status_code)

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
app.include_router(comments.router)
app.include_router(stripe_webhooks.router)
app.include_router(announcements.router)
app.include_router(profiles.router)
app.include_router(two_factor.router)
app.include_router(saved_searches.router)
app.include_router(api_key_usage.router)
app.include_router(skill_quiz.router)
app.include_router(requester_onboarding.router)
app.include_router(webhook_templates.router)
app.include_router(task_dependencies.router)
app.include_router(endorsements.router)
app.include_router(worker_marketplace.router)
app.include_router(ratings.router)
app.include_router(ratings.worker_router)
app.include_router(portfolio.router)
app.include_router(portfolio.public_router)
app.include_router(requester_templates.router)
app.include_router(requester_templates.tasks_alias_router)
app.include_router(requester_templates.marketplace_router)
app.include_router(worker_teams.router)
app.include_router(worker_teams.tasks_router)
app.include_router(applications.router)
app.include_router(availability.router)
app.include_router(task_messages.router)
app.include_router(notification_digest.router)
app.include_router(global_search.router)
app.include_router(oauth.router)
app.include_router(platform_stats.router)
app.include_router(leagues.router)
app.include_router(quests.router)

# ─── Health ───────────────────────────────────────────────────────────────

@app.get("/health", tags=["health"])
async def health():
    """Basic liveness probe — does not check DB (used by Docker healthcheck)."""
    return {
        "status": "ok",
        "version": settings.app_version,
        "service": "crowdsourcerer-api",
    }


@app.get("/health/ready", tags=["health"])
async def health_ready():
    """Readiness probe — checks DB connectivity.  Returns 503 if DB is unreachable."""
    from sqlalchemy import text
    db_ok = False
    db_error: str | None = None
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception as exc:
        db_error = str(exc)

    status_code = 200 if db_ok else 503
    body = {
        "status": "ready" if db_ok else "degraded",
        "version": settings.app_version,
        "checks": {
            "database": {"ok": db_ok, "error": db_error},
        },
    }
    return JSONResponse(content=body, status_code=status_code)


@app.get("/v1/health", tags=["health"])
async def health_v1():
    """Public health + config diagnostic — no auth required."""
    from sqlalchemy import text
    db_ok = False
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass
    return {
        "status": "ok",
        "version": settings.app_version,
        "database": db_ok,
        "config": {
            "rebasekit_api_key": bool(settings.rebasekit_api_key),
            "rebasekit_base_url": settings.rebasekit_base_url,
            "jwt_secret": settings.jwt_secret != "change-me-in-production",
            "stripe": bool(settings.stripe_secret_key),
            "email_enabled": settings.email_enabled,
            "google_oauth": bool(settings.google_client_id),
        },
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

# ─── Global error handler ─────────────────────────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled_error", path=request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_error", "message": "An unexpected error occurred"},
    )
