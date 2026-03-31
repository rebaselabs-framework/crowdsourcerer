/**
 * Public pages — verify all unauthenticated pages render correctly.
 * Tests page content, key elements, and navigation links.
 */
import { test, expect } from "@playwright/test";
import { assertNoServerError, assertLayoutLoaded } from "./helpers";

test.describe("Public pages", () => {
  test("pricing page shows plans and credit bundles", async ({ page }) => {
    await page.goto("/pricing");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const heading = page.locator("h1");
    await expect(heading).toBeVisible();

    // Should show pricing info — credits or plans
    const body = await page.textContent("body");
    expect(body?.toLowerCase()).toMatch(/credit|plan|pricing|free/);
  });

  test("marketplace page renders", async ({ page }) => {
    await page.goto("/marketplace");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const heading = page.locator("h1");
    await expect(heading).toBeVisible();
  });

  test("tasks page renders", async ({ page }) => {
    await page.goto("/tasks");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("leaderboard page renders", async ({ page }) => {
    await page.goto("/leaderboard");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("docs page renders", async ({ page }) => {
    await page.goto("/docs");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const body = await page.textContent("body");
    expect(body?.toLowerCase()).toMatch(/api|documentation|docs|reference/);
  });

  test("docs/api-reference page renders", async ({ page }) => {
    await page.goto("/docs/api-reference");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("forgot-password page renders", async ({ page }) => {
    await page.goto("/forgot-password");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    await expect(page.locator('input[name="email"], input[type="email"]')).toBeVisible();
  });

  test("use-cases index page renders", async ({ page }) => {
    await page.goto("/use-cases");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  const useCases = [
    "content-moderation",
    "data-transformation",
    "document-parsing",
    "entity-extraction",
    "web-research",
  ];

  for (const uc of useCases) {
    test(`use-case: ${uc} page renders`, async ({ page }) => {
      await page.goto(`/use-cases/${uc}`);
      await assertNoServerError(page);
      await assertLayoutLoaded(page);
    });
  }

  test("workers browse page renders", async ({ page }) => {
    await page.goto("/workers/browse");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });
});
