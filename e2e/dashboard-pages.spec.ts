/**
 * Dashboard pages coverage — verify all untested requester dashboard pages render.
 *
 * Previously untested routes:
 *   /dashboard/notification-preferences, /dashboard/batch-upload,
 *   /dashboard/disputes, /dashboard/experiments, /dashboard/export,
 *   /dashboard/marketplace, /dashboard/marketplace/new, /dashboard/quality,
 *   /dashboard/quota, /dashboard/referrals, /dashboard/requester-onboarding,
 *   /dashboard/requester, /dashboard/revenue, /dashboard/review,
 *   /dashboard/saved-searches, /dashboard/scheduled, /dashboard/search,
 *   /dashboard/search/tasks, /dashboard/sla, /dashboard/task-templates,
 *   /dashboard/template-marketplace, /dashboard/triggers,
 *   /dashboard/worker/skill-quiz
 *
 * Uses the global-setup requester account (storageState).
 */
import { test, expect } from "@playwright/test";
import { assertNoServerError, assertLayoutLoaded } from "./helpers";
import { REQUESTER_STATE_PATH } from "./global-setup";

test.describe("Dashboard pages (previously untested)", () => {
  test.beforeEach(async ({ page, context }) => {
    const fs = await import("fs");
    const state = JSON.parse(fs.readFileSync(REQUESTER_STATE_PATH, "utf8"));
    if (state.cookies) {
      await context.addCookies(state.cookies);
    }
  });

  test("notification-preferences page renders", async ({ page }) => {
    await page.goto("/dashboard/notification-preferences");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("batch-upload page renders", async ({ page }) => {
    await page.goto("/dashboard/batch-upload");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("disputes page renders", async ({ page }) => {
    await page.goto("/dashboard/disputes");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("experiments page renders", async ({ page }) => {
    await page.goto("/dashboard/experiments");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("export page renders", async ({ page }) => {
    await page.goto("/dashboard/export");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("marketplace page renders", async ({ page }) => {
    await page.goto("/dashboard/marketplace");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("marketplace/new page renders", async ({ page }) => {
    await page.goto("/dashboard/marketplace/new");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("quality page renders", async ({ page }) => {
    await page.goto("/dashboard/quality");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("quota page renders", async ({ page }) => {
    await page.goto("/dashboard/quota");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("referrals page renders", async ({ page }) => {
    await page.goto("/dashboard/referrals");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("requester-onboarding page renders", async ({ page }) => {
    await page.goto("/dashboard/requester-onboarding");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("requester hub page renders", async ({ page }) => {
    await page.goto("/dashboard/requester");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("revenue page renders", async ({ page }) => {
    await page.goto("/dashboard/revenue");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("review page renders", async ({ page }) => {
    await page.goto("/dashboard/review");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("saved-searches page renders", async ({ page }) => {
    await page.goto("/dashboard/saved-searches");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("scheduled page renders", async ({ page }) => {
    await page.goto("/dashboard/scheduled");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("search page renders", async ({ page }) => {
    await page.goto("/dashboard/search");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("search/tasks page renders", async ({ page }) => {
    await page.goto("/dashboard/search/tasks");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("SLA page renders", async ({ page }) => {
    await page.goto("/dashboard/sla");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("task-templates page renders", async ({ page }) => {
    await page.goto("/dashboard/task-templates");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("template-marketplace page renders", async ({ page }) => {
    await page.goto("/dashboard/template-marketplace");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("triggers page renders", async ({ page }) => {
    await page.goto("/dashboard/triggers");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("worker skill-quiz page renders", async ({ page }) => {
    await page.goto("/dashboard/worker/skill-quiz");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });
});
