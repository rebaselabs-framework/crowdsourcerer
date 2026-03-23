"""CrowdSorcerer API — main entrypoint."""
from __future__ import annotations
import time
import uuid

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from core.config import get_settings
from core.database import Base, engine, AsyncSessionLocal
from core.sweeper import start_sweeper, stop_sweeper
from routers import auth, credits, tasks, users, worker, leaderboard, badges, challenges, quality, admin, webhooks, payouts, referrals, notifications, skills
from workers.base import get_rebasekit_client

settings = get_settings()
logger = structlog.get_logger()

# ─── Rate limiting ────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])

app = FastAPI(
    title="CrowdSorcerer API",
    description="AI-native task crowdsourcing platform powered by RebaseKit",
    version=settings.app_version,
    docs_url="/docs",
    redoc_url="/redoc",
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
    }

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
