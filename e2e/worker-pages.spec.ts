/**
 * Worker pages coverage — verify all untested worker pages render without errors.
 *
 * These pages were identified as having zero E2E coverage:
 *   /worker/applications, /worker/availability, /worker/invites,
 *   /worker/leagues, /worker/messages, /worker/performance,
 *   /worker/profile-setup, /worker/quests, /worker/ratings,
 *   /worker/recommendations, /worker/referrals, /worker/saved-searches,
 *   /worker/submitted, /worker/teams, /worker/watchlist
 *
 * Uses the global-setup worker account (storageState).
 */
import { test, expect } from "@playwright/test";
import { assertNoServerError, assertLayoutLoaded } from "./helpers";
import { WORKER_STATE_PATH } from "./global-setup";

test.describe("Worker pages (previously untested)", () => {
  test.beforeEach(async ({ page, context }) => {
    const fs = await import("fs");
    const state = JSON.parse(fs.readFileSync(WORKER_STATE_PATH, "utf8"));
    if (state.cookies) {
      await context.addCookies(state.cookies);
    }
  });

  test("worker applications page renders", async ({ page }) => {
    await page.goto("/worker/applications");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker availability page renders", async ({ page }) => {
    await page.goto("/worker/availability");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker invites page renders", async ({ page }) => {
    await page.goto("/worker/invites");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker leagues page renders", async ({ page }) => {
    await page.goto("/worker/leagues");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker messages page renders", async ({ page }) => {
    await page.goto("/worker/messages");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker performance page renders", async ({ page }) => {
    await page.goto("/worker/performance");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker profile-setup page renders", async ({ page }) => {
    await page.goto("/worker/profile-setup");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker quests page renders", async ({ page }) => {
    await page.goto("/worker/quests");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker ratings page renders", async ({ page }) => {
    await page.goto("/worker/ratings");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker recommendations page renders", async ({ page }) => {
    await page.goto("/worker/recommendations");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker referrals page renders", async ({ page }) => {
    await page.goto("/worker/referrals");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker saved-searches page renders", async ({ page }) => {
    await page.goto("/worker/saved-searches");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker submitted page renders", async ({ page }) => {
    await page.goto("/worker/submitted");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker teams page renders", async ({ page }) => {
    await page.goto("/worker/teams");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker watchlist page renders", async ({ page }) => {
    await page.goto("/worker/watchlist");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });
});
