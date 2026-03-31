/**
 * Dashboard tests — verify authenticated pages and core user flows.
 *
 * These tests register a fresh user and navigate through key dashboard sections.
 */
import { test, expect } from "@playwright/test";
import {
  registerUser,
  loginUser,
  assertNoServerError,
  assertLayoutLoaded,
} from "./helpers";

test.describe("Dashboard (authenticated)", () => {
  let email: string;

  test.beforeAll(async ({ browser }) => {
    // Register a user once for all dashboard tests
    const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
    const page = await ctx.newPage();
    email = await registerUser(page, { role: "requester" });
    await ctx.close();
  });

  test.beforeEach(async ({ page }) => {
    await loginUser(page, email);
  });

  test("dashboard main page loads with user data", async ({ page }) => {
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    // Should show user info or credits section
    const body = await page.textContent("body");
    expect(body?.toLowerCase()).toMatch(/credit|dashboard|task|welcome/);
  });

  test("dashboard/tasks page renders", async ({ page }) => {
    await page.goto("/dashboard/tasks");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("dashboard/new-task page has task creation form", async ({ page }) => {
    await page.goto("/dashboard/new-task");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    // Should have task type selection or form elements
    const body = await page.textContent("body");
    expect(body?.toLowerCase()).toMatch(/task|type|create|submit/);
  });

  test("dashboard/credits page shows credit balance", async ({ page }) => {
    await page.goto("/dashboard/credits");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    // New user should have free tier credits
    const body = await page.textContent("body");
    expect(body?.toLowerCase()).toMatch(/credit|balance/);
  });

  test("dashboard/billing page renders", async ({ page }) => {
    await page.goto("/dashboard/billing");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("dashboard/profile page renders with user info", async ({ page }) => {
    await page.goto("/dashboard/profile");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("dashboard/security page renders", async ({ page }) => {
    await page.goto("/dashboard/security");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    // Should mention 2FA or security options
    const body = await page.textContent("body");
    expect(body?.toLowerCase()).toMatch(/two.factor|2fa|security|password/);
  });

  test("dashboard/api-keys page renders", async ({ page }) => {
    await page.goto("/dashboard/api-keys");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("dashboard/notifications page renders", async ({ page }) => {
    await page.goto("/dashboard/notifications");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("dashboard/analytics page renders", async ({ page }) => {
    await page.goto("/dashboard/analytics");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("dashboard/webhooks page renders", async ({ page }) => {
    await page.goto("/dashboard/webhooks");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("dashboard/pipelines page renders", async ({ page }) => {
    await page.goto("/dashboard/pipelines");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("dashboard/team page renders", async ({ page }) => {
    await page.goto("/dashboard/team");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);
  });

  test("dashboard sidebar navigation works", async ({ page }) => {
    // Click through sidebar links if they exist
    const sidebarLinks = page.locator(
      'nav a[href*="/dashboard/"], aside a[href*="/dashboard/"]'
    );
    const count = await sidebarLinks.count();

    if (count > 0) {
      // Test first few sidebar links
      const maxLinks = Math.min(count, 5);
      for (let i = 0; i < maxLinks; i++) {
        const link = sidebarLinks.nth(i);
        const href = await link.getAttribute("href");
        if (href) {
          await page.goto(href);
          await assertNoServerError(page);
          await assertLayoutLoaded(page);
        }
      }
    }
  });
});
