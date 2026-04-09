#!/usr/bin/env python3
"""
API Performance Profiler for CrowdSorcerer.

Registers a test account, then hits every major endpoint and measures response times.
Groups results by speed tier and identifies endpoints needing optimization.
"""

import asyncio
import time
import json
import sys
import statistics
from dataclasses import dataclass, field
from typing import Optional

import httpx

BASE = "https://crowdsourcerer.rebaselabs.online"
TIMEOUT = 15.0


@dataclass
class Result:
    method: str
    path: str
    status: int
    time_ms: float
    error: Optional[str] = None


@dataclass
class ProfileRun:
    results: list[Result] = field(default_factory=list)

    def add(self, r: Result):
        self.results.append(r)

    def report(self):
        """Print a performance report grouped by speed tier."""
        ok = [r for r in self.results if r.error is None]
        errs = [r for r in self.results if r.error is not None]

        if not ok:
            print("No successful requests!")
            return

        times = [r.time_ms for r in ok]
        print(f"\n{'='*70}")
        print(f"  CrowdSorcerer API Performance Profile")
        print(f"  {len(ok)} endpoints profiled, {len(errs)} errors")
        print(f"{'='*70}")
        print(f"  Mean: {statistics.mean(times):.0f}ms | Median: {statistics.median(times):.0f}ms | P95: {sorted(times)[int(len(times)*0.95)]:.0f}ms | P99: {sorted(times)[int(len(times)*0.99)]:.0f}ms")
        print(f"  Min: {min(times):.0f}ms | Max: {max(times):.0f}ms")

        # Speed tiers
        tiers = [
            ("🟢 FAST (<100ms)", [r for r in ok if r.time_ms < 100]),
            ("🟡 OK (100-300ms)", [r for r in ok if 100 <= r.time_ms < 300]),
            ("🟠 SLOW (300-1000ms)", [r for r in ok if 300 <= r.time_ms < 1000]),
            ("🔴 CRITICAL (>1000ms)", [r for r in ok if r.time_ms >= 1000]),
        ]

        for label, tier_results in tiers:
            if not tier_results:
                continue
            print(f"\n  {label} ({len(tier_results)} endpoints)")
            print(f"  {'─'*60}")
            for r in sorted(tier_results, key=lambda x: -x.time_ms):
                print(f"    {r.time_ms:6.0f}ms  {r.method:6s} {r.path}  [{r.status}]")

        if errs:
            print(f"\n  ⚠️  ERRORS ({len(errs)} endpoints)")
            print(f"  {'─'*60}")
            for r in errs:
                print(f"    {r.method:6s} {r.path}  — {r.error}")

        # Top 10 slowest
        print(f"\n  {'='*70}")
        print(f"  TOP 10 SLOWEST ENDPOINTS")
        print(f"  {'='*70}")
        for r in sorted(ok, key=lambda x: -x.time_ms)[:10]:
            print(f"    {r.time_ms:6.0f}ms  {r.method:6s} {r.path}  [{r.status}]")

        print()


async def timed_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    headers: dict | None = None,
    json_data: dict | None = None,
    params: dict | None = None,
) -> Result:
    """Make a request and measure response time."""
    url = f"{BASE}{path}"
    t0 = time.monotonic()
    try:
        resp = await client.request(
            method,
            url,
            headers=headers or {},
            json=json_data,
            params=params,
            timeout=TIMEOUT,
        )
        elapsed = (time.monotonic() - t0) * 1000
        return Result(method=method, path=path, status=resp.status_code, time_ms=elapsed)
    except Exception as e:
        elapsed = (time.monotonic() - t0) * 1000
        return Result(method=method, path=path, status=0, time_ms=elapsed, error=str(e))


async def main():
    run = ProfileRun()

    async with httpx.AsyncClient(verify=False) as client:
        # ── Phase 1: Public endpoints (no auth) ──────────────────────────

        print("Phase 1: Public endpoints...")
        public_endpoints = [
            ("GET", "/"),
            ("GET", "/health"),
            ("GET", "/health/ready"),
            ("GET", "/v1/health"),
            ("GET", "/v1/config"),
            ("GET", "/v1/openapi-spec"),
            ("GET", "/v1/tasks/public"),
            ("GET", "/v1/tasks/templates"),
            ("GET", "/v1/platform/stats"),
            ("GET", "/v1/leaderboard"),
            ("GET", "/v1/leagues/tiers"),
            ("GET", "/v1/leagues/current"),
            ("GET", "/v1/certifications"),
            ("GET", "/v1/marketplace/templates"),
            ("GET", "/v1/marketplace/categories"),
            ("GET", "/v1/workers/browse"),
            ("GET", "/v1/sla/priorities"),
            ("GET", "/v1/announcements"),
            ("GET", "/v1/template-marketplace"),
        ]

        for method, path in public_endpoints:
            r = await timed_request(client, method, path)
            run.add(r)
            print(f"  {r.time_ms:6.0f}ms  {method} {path}  [{r.status}]")

        # ── Phase 2: Register test account ────────────────────────────────

        print("\nPhase 2: Registering test account...")
        email = f"perftest-{int(time.time())}@example.com"
        password = "PerfT3st!Pass"

        r = await timed_request(
            client, "POST", "/v1/auth/register",
            json_data={"email": email, "password": password, "name": "Perf Test", "role": "requester"},
        )
        run.add(r)
        print(f"  {r.time_ms:6.0f}ms  POST /v1/auth/register  [{r.status}]")

        if r.status != 201:
            # Try login instead
            r2 = await timed_request(
                client, "POST", "/v1/auth/login",
                json_data={"email": email, "password": password},
            )
            if r2.status == 200:
                resp = await client.post(f"{BASE}/v1/auth/login", json={"email": email, "password": password})
                token = resp.json().get("access_token", "")
            else:
                print("  ⚠️  Could not authenticate — skipping authed endpoints")
                run.report()
                return
        else:
            resp = await client.post(
                f"{BASE}/v1/auth/register",
                json={"email": f"perftest2-{int(time.time())}@example.com", "password": password, "name": "Perf2", "role": "requester"},
            )
            # Use the first successful registration
            reg_resp = await client.post(
                f"{BASE}/v1/auth/login",
                json={"email": email, "password": password},
            )
            token = reg_resp.json().get("access_token", "")

        if not token:
            # Fallback: re-register
            email3 = f"perftest3-{int(time.time())}@example.com"
            reg3 = await client.post(
                f"{BASE}/v1/auth/register",
                json={"email": email3, "password": password, "name": "Perf3", "role": "requester"},
            )
            if reg3.status_code == 201:
                token = reg3.json().get("access_token", "")

        if not token:
            print("  ⚠️  No token obtained — skipping authed endpoints")
            run.report()
            return

        auth = {"Authorization": f"Bearer {token}"}
        print(f"  ✓ Authenticated as {email}")

        # ── Phase 3: Requester endpoints ──────────────────────────────────

        print("\nPhase 3: Requester endpoints...")
        requester_endpoints = [
            ("GET", "/v1/users/me"),
            ("GET", "/v1/credits"),
            ("GET", "/v1/credits/transactions"),
            ("GET", "/v1/api-keys"),
            ("GET", "/v1/scopes"),
            ("GET", "/v1/quota"),
            ("GET", "/v1/users/credit-alert"),
            ("GET", "/v1/users/me/profile-status"),
            ("GET", "/v1/tasks"),
            ("GET", "/v1/tasks/tags"),
            ("GET", "/v1/tasks/scheduled"),
            ("GET", "/v1/tasks/review-summary"),
            ("GET", "/v1/tasks/sla-status"),
            ("GET", "/v1/notifications"),
            ("GET", "/v1/notifications/unread-count"),
            ("GET", "/v1/notifications/grouped"),
            ("GET", "/v1/notifications/preferences"),
            ("GET", "/v1/notifications/digest-prefs"),
            ("GET", "/v1/webhooks/events"),
            ("GET", "/v1/webhooks/endpoints"),
            ("GET", "/v1/webhooks/logs"),
            ("GET", "/v1/webhooks/stats"),
            ("GET", "/v1/webhooks/preferences"),
            ("GET", "/v1/webhooks/payload-templates"),
            ("GET", "/v1/orgs"),
            ("GET", "/v1/pipelines"),
            ("GET", "/v1/analytics/overview"),
            ("GET", "/v1/analytics/costs"),
            ("GET", "/v1/analytics/completion-times"),
            ("GET", "/v1/analytics/revenue"),
            ("GET", "/v1/experiments"),
            ("GET", "/v1/referrals/stats"),
            ("GET", "/v1/referrals"),
            ("GET", "/v1/onboarding/status"),
            ("GET", "/v1/requester-onboarding/status"),
            ("GET", "/v1/search/tasks"),
            ("GET", "/v1/search/global"),
            ("GET", "/v1/search"),
            ("GET", "/v1/disputes/tasks"),
            ("GET", "/v1/task-templates"),
            ("GET", "/v1/api-keys/usage/overview"),
            ("GET", "/v1/quests"),
        ]

        for method, path in requester_endpoints:
            r = await timed_request(client, method, path, headers=auth)
            run.add(r)
            print(f"  {r.time_ms:6.0f}ms  {method} {path}  [{r.status}]")

        # ── Phase 4: Create a task and profile task-related endpoints ─────

        print("\nPhase 4: Task-specific endpoints...")
        task_r = await timed_request(
            client, "POST", "/v1/tasks",
            headers=auth,
            json_data={
                "type": "web_research",
                "input": {"url": "https://example.com", "instruction": "Extract title"},
            },
        )
        run.add(task_r)
        print(f"  {task_r.time_ms:6.0f}ms  POST /v1/tasks  [{task_r.status}]")

        task_id = None
        if task_r.status == 201:
            resp = await client.post(
                f"{BASE}/v1/tasks",
                headers=auth,
                json={"type": "llm_generate", "input": {"prompt": "Hello"}},
            )
            if resp.status_code == 201:
                task_id = resp.json().get("task_id") or resp.json().get("id")

            # Also get from first task
            tasks_resp = await client.get(f"{BASE}/v1/tasks", headers=auth)
            if tasks_resp.status_code == 200:
                body = tasks_resp.json()
                items = body.get("items") or body.get("tasks") or body
                if isinstance(items, list) and items:
                    task_id = items[0].get("id") or items[0].get("task_id")

        if task_id:
            task_endpoints = [
                ("GET", f"/v1/tasks/{task_id}"),
                ("GET", f"/v1/tasks/{task_id}/duplicate-params"),
                ("GET", f"/v1/tasks/{task_id}/submissions"),
                ("GET", f"/v1/tasks/{task_id}/analytics"),
                ("GET", f"/v1/tasks/{task_id}/related"),
                ("GET", f"/v1/tasks/{task_id}/suggested-workers"),
                ("GET", f"/v1/tasks/{task_id}/comments"),
                ("GET", f"/v1/tasks/{task_id}/dependencies"),
                ("GET", f"/v1/tasks/{task_id}/dependents"),
                ("GET", f"/v1/tasks/{task_id}/messages"),
                ("GET", f"/v1/tasks/{task_id}/invites"),
                ("GET", f"/v1/tasks/{task_id}/applications"),
            ]
            for method, path in task_endpoints:
                r = await timed_request(client, method, path, headers=auth)
                run.add(r)
                print(f"  {r.time_ms:6.0f}ms  {method} {path}  [{r.status}]")

        # ── Phase 5: Worker endpoints (enroll first) ──────────────────────

        print("\nPhase 5: Worker endpoints...")
        # Register a worker account
        worker_email = f"perfworker-{int(time.time())}@example.com"
        wreg = await client.post(
            f"{BASE}/v1/auth/register",
            json={"email": worker_email, "password": password, "name": "PerfWorker", "role": "worker"},
        )
        worker_token = ""
        if wreg.status_code == 201:
            worker_token = wreg.json().get("access_token", "")
        else:
            wlogin = await client.post(
                f"{BASE}/v1/auth/login",
                json={"email": worker_email, "password": password},
            )
            if wlogin.status_code == 200:
                worker_token = wlogin.json().get("access_token", "")

        if worker_token:
            wauth = {"Authorization": f"Bearer {worker_token}"}

            # Enroll as worker
            await client.post(f"{BASE}/v1/worker/enroll", headers=wauth)

            worker_endpoints = [
                ("GET", "/v1/worker/profile"),
                ("GET", "/v1/worker/interests"),
                ("GET", "/v1/worker/stats"),
                ("GET", "/v1/worker/performance"),
                ("GET", "/v1/worker/activity/calendar"),
                ("GET", "/v1/worker/tasks/feed"),
                ("GET", "/v1/worker/tasks"),
                ("GET", "/v1/worker/earnings/analytics"),
                ("GET", "/v1/worker/assignments"),
                ("GET", "/v1/worker/activity"),
                ("GET", "/v1/worker/recommendations"),
                ("GET", "/v1/worker/badges"),
                ("GET", "/v1/worker/portfolio"),
                ("GET", "/v1/worker/invites"),
                ("GET", "/v1/worker/watchlist"),
                ("GET", "/v1/worker/applications"),
                ("GET", "/v1/worker/saved-searches"),
                ("GET", "/v1/worker/availability"),
                ("GET", "/v1/worker/skill-quiz/categories"),
                ("GET", "/v1/worker/skill-quiz/attempts"),
                ("GET", "/v1/workers/me/skills"),
                ("GET", "/v1/workers/me/recommended"),
                ("GET", "/v1/reputation/me"),
                ("GET", "/v1/challenges/today"),
                ("GET", "/v1/challenges/history"),
                ("GET", "/v1/tasks/messages/unread-count"),
                ("GET", "/v1/tasks/messages/inbox"),
                ("GET", "/v1/leagues/history"),
                ("GET", "/v1/certifications/me/earned"),
                ("GET", "/v1/payouts"),
                ("GET", "/v1/payouts/summary"),
            ]

            for method, path in worker_endpoints:
                r = await timed_request(client, method, path, headers=wauth)
                run.add(r)
                print(f"  {r.time_ms:6.0f}ms  {method} {path}  [{r.status}]")

        # ── Phase 6: Search & export with params ──────────────────────────

        print("\nPhase 6: Search endpoints with query params...")
        search_tests = [
            ("GET", "/v1/search/tasks", {"q": "research"}),
            ("GET", "/v1/search/tasks", {"q": "test", "status": "pending"}),
            ("GET", "/v1/search/global", {"q": "example"}),
            ("GET", "/v1/search", {"q": "research", "entity_type": "task"}),
            ("GET", "/v1/tasks/export", {"format": "json"}),
        ]

        for method, path, params in search_tests:
            r = await timed_request(client, method, path, headers=auth, params=params)
            run.add(r)
            print(f"  {r.time_ms:6.0f}ms  {method} {path}?{params}  [{r.status}]")

        # ── Phase 7: Auth flow timing ─────────────────────────────────────

        print("\nPhase 7: Auth flow timing...")
        login_r = await timed_request(
            client, "POST", "/v1/auth/login",
            json_data={"email": email, "password": password},
        )
        run.add(login_r)
        print(f"  {login_r.time_ms:6.0f}ms  POST /v1/auth/login  [{login_r.status}]")

        # ── Report ────────────────────────────────────────────────────────

        run.report()


if __name__ == "__main__":
    asyncio.run(main())
