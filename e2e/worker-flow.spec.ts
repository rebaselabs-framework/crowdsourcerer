/**
 * Worker flow tests — verify the worker experience.
 *
 * Register as a worker, go through onboarding, check worker pages.
 */
import { test, expect } from "@playwright/test";
import {
  registerUser,
  loginUser,
  assertNoServerError,
  assertLayoutLoaded,
} from "./helpers";

test.describe("Worker flows", () => {
  let email: string;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
    const page = await ctx.newPage();
    email = await registerUser(page, { role: "worker" });
    await ctx.close();
  });

  test.beforeEach(async ({ page }) => {
    await loginUser(page, email);
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
