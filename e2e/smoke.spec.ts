/**
 * Smoke tests — verify the deployed app is alive and serving pages.
 * These are the fastest, most basic checks. If smoke fails, everything else will too.
 */
import { test, expect } from "@playwright/test";
import { assertNoServerError, assertLayoutLoaded } from "./helpers";

test.describe("Smoke tests", () => {
  test("homepage loads and has correct title", async ({ page }) => {
    const response = await page.goto("/");
    expect(response?.status()).toBeLessThan(500);
    await assertLayoutLoaded(page);

    // Title should mention CrowdSorcerer
    const title = await page.title();
    expect(title.toLowerCase()).toContain("crowdsorcerer");
  });

  test("homepage has primary CTA buttons", async ({ page }) => {
    await page.goto("/");
    await assertNoServerError(page);

    // Should have sign-up / get-started CTA
    const ctaLinks = page.locator('a[href*="register"], a[href*="login"]');
    await expect(ctaLinks.first()).toBeVisible();
  });

  test("login page loads", async ({ page }) => {
    await page.goto("/login");
    await expect(page.locator("h1")).toContainText("Welcome back");
    await expect(page.locator('input[name="email"]')).toBeVisible();
    await expect(page.locator('input[name="password"]')).toBeVisible();
    await expect(page.locator('button[type="submit"]')).toBeVisible();
  });

  test("register page loads", async ({ page }) => {
    await page.goto("/register");
    await expect(page.locator("h1")).toContainText("Create your account");
    await expect(page.locator('input[name="name"]')).toBeVisible();
    await expect(page.locator('input[name="email"]')).toBeVisible();
    await expect(page.locator('input[name="password"]')).toBeVisible();
  });

  test("API health check responds", async ({ page }) => {
    // The web app proxies /api/* to the backend
    // Try the platform stats endpoint which is public
    const response = await page.goto("/v1/platform/stats");
    // Should either succeed or 404 (if proxied differently), but not 500
    expect(response?.status()).toBeLessThan(500);
  });

  test("no console errors on homepage", async ({ page }) => {
    const consoleErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") {
        consoleErrors.push(msg.text());
      }
    });

    await page.goto("/");
    await page.waitForLoadState("networkidle");

    // Filter out known benign errors (e.g., favicon, analytics)
    const realErrors = consoleErrors.filter(
      (e) =>
        !e.includes("favicon") &&
        !e.includes("analytics") &&
        !e.includes("ERR_BLOCKED_BY_CLIENT") // ad blockers
    );
    expect(realErrors).toHaveLength(0);
  });
});
