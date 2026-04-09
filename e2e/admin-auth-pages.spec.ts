/**
 * Admin pages + auth edge-case pages — verify rendering.
 *
 * Admin pages: /admin/* (18 pages, all previously untested)
 * Auth pages: /reset-password, /verify-email, /login-2fa,
 *             /auth/google-success (4 pages, all previously untested)
 * Public: /widget, /workers/[id], /docs/api, /docs/sandbox (4 pages)
 *
 * Admin pages require admin auth — tested as unauthenticated visitor
 * (should redirect to login or show access denied, NOT crash with 500).
 *
 * Auth edge-case pages are tested without valid tokens — they should
 * render gracefully (show form or error message, not crash).
 */
import { test, expect } from "@playwright/test";
import { assertNoServerError, assertLayoutLoaded } from "./helpers";
import { REQUESTER_STATE_PATH } from "./global-setup";

// ── Admin pages (expect redirect to login or 403, not 500) ──────────────────

test.describe("Admin pages (no admin auth)", () => {
  const adminPages = [
    "/admin",
    "/admin/alerts",
    "/admin/analytics",
    "/admin/announcements",
    "/admin/audit-log",
    "/admin/billing",
    "/admin/cache",
    "/admin/health",
    "/admin/onboarding-funnel",
    "/admin/payouts",
    "/admin/quality",
    "/admin/queue",
    "/admin/reputation",
    "/admin/setup",
    "/admin/tasks",
    "/admin/users",
    "/admin/worker-onboarding-funnel",
    "/admin/workers",
  ];

  for (const path of adminPages) {
    test(`${path} does not crash (no 500)`, async ({ page }) => {
      const response = await page.goto(path);
      // Admin pages should either:
      // - redirect to /login (302/303)
      // - show access denied (403)
      // - render the page (200) with a login prompt
      // But NEVER return 500
      expect(response?.status()).toBeLessThan(500);
    });
  }
});

// ── Admin pages with requester auth (non-admin) ────────────────────────────

test.describe("Admin pages (non-admin requester)", () => {
  test.beforeEach(async ({ page, context }) => {
    const fs = await import("fs");
    const state = JSON.parse(fs.readFileSync(REQUESTER_STATE_PATH, "utf8"));
    if (state.cookies) {
      await context.addCookies(state.cookies);
    }
  });

  const adminPages = [
    "/admin",
    "/admin/users",
    "/admin/tasks",
    "/admin/analytics",
  ];

  for (const path of adminPages) {
    test(`${path} with non-admin user does not crash`, async ({ page }) => {
      const response = await page.goto(path);
      // Should either deny access or redirect, not crash
      expect(response?.status()).toBeLessThan(500);
    });
  }
});

// ── Auth edge-case pages ──────────────────────────────────────────────────

test.describe("Auth edge-case pages", () => {
  test("reset-password page renders without token", async ({ page }) => {
    await page.goto("/reset-password");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("verify-email page renders without token", async ({ page }) => {
    await page.goto("/verify-email");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("login-2fa page renders", async ({ page }) => {
    await page.goto("/login-2fa");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("google-success page renders without params", async ({ page }) => {
    await page.goto("/auth/google-success");
    // Should handle missing token gracefully (redirect or show error)
    await assertNoServerError(page);
  });
});

// ── Public pages with no auth ─────────────────────────────────────────────

test.describe("Public pages (previously untested)", () => {
  test("widget page renders", async ({ page }) => {
    const response = await page.goto("/widget");
    expect(response?.status()).toBeLessThan(500);
    await assertLayoutLoaded(page);
  });

  test("docs/api page renders", async ({ page }) => {
    await page.goto("/docs/api");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("docs/sandbox page renders", async ({ page }) => {
    await page.goto("/docs/sandbox");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("orgs/join page renders without token", async ({ page }) => {
    await page.goto("/orgs/join");
    // Should handle missing token gracefully
    await assertNoServerError(page);
  });
});
