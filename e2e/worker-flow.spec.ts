/**
 * Worker flow tests — verify the worker experience.
 *
 * Uses the global-setup worker account (storageState) instead of
 * registering a new account. This avoids rate limit issues.
 */
import { test, expect } from "@playwright/test";
import {
  assertNoServerError,
  assertLayoutLoaded,
} from "./helpers";
import { WORKER_STATE_PATH } from "./global-setup";

test.describe("Worker flows", () => {
  test.beforeEach(async ({ page, context }) => {
    // Inject saved cookies from global setup
    const fs = await import("fs");
    const state = JSON.parse(fs.readFileSync(WORKER_STATE_PATH, "utf8"));
    if (state.cookies) {
      await context.addCookies(state.cookies);
    }
  });

  test("worker dashboard loads", async ({ page }) => {
    await page.goto("/worker");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker marketplace page renders", async ({ page }) => {
    await page.goto("/worker/marketplace");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker skills page renders", async ({ page }) => {
    await page.goto("/worker/skills");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker earnings page renders", async ({ page }) => {
    await page.goto("/worker/earnings");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker achievements page renders", async ({ page }) => {
    await page.goto("/worker/achievements");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker reputation page renders", async ({ page }) => {
    await page.goto("/worker/reputation");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker portfolio page renders", async ({ page }) => {
    await page.goto("/worker/portfolio");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker certifications page renders", async ({ page }) => {
    await page.goto("/worker/certifications");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker challenges page renders", async ({ page }) => {
    await page.goto("/worker/challenges");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker leaderboard page renders", async ({ page }) => {
    await page.goto("/worker/leaderboard");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker activity page renders", async ({ page }) => {
    await page.goto("/worker/activity");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker notifications page renders", async ({ page }) => {
    await page.goto("/worker/notifications");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker onboarding page renders", async ({ page }) => {
    await page.goto("/worker/onboarding");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });
});
