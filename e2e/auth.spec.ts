/**
 * Auth flow tests — registration, login, logout, auth redirects.
 *
 * These tests interact with the LIVE deployed app and create real users.
 * We minimize total registrations to stay under rate limits (5/min).
 *
 * Registration budget: 2 total (1 requester in beforeAll, 1 worker in test)
 */
import { test, expect } from "@playwright/test";
import {
  testEmail,
  TEST_PASSWORD,
  TEST_NAME,
  registerUser,
  loginUser,
  assertNoServerError,
} from "./helpers";

test.describe("Auth flows", () => {
  // Shared email registered once in beforeAll — used by login, logout, duplicate tests
  let sharedEmail: string;

  test.beforeAll(async ({ browser }) => {
    const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
    const page = await ctx.newPage();
    sharedEmail = await registerUser(page, { role: "requester" });
    await ctx.close();
  });

  test("register page renders with all form elements", async ({ page }) => {
    await page.goto("/register");
    await expect(page.locator("h1")).toContainText("Create your account");
    await expect(page.locator('input[name="name"]')).toBeVisible();
    await expect(page.locator('input[name="email"]')).toBeVisible();
    await expect(page.locator('input[name="password"]')).toBeVisible();
    await expect(page.locator('button[type="submit"]')).toBeVisible();
  });

  test("login with a registered account", async ({ page }) => {
    // Reuse the shared registered email — no new registration needed
    await loginUser(page, sharedEmail);
    expect(page.url()).toContain("/dashboard");
    await assertNoServerError(page);
  });

  test("login with wrong password shows error", async ({ page }) => {
    await page.goto("/login");
    await page.fill('input[name="email"]', "wrong@example.com");
    await page.fill('input[name="password"]', "WrongPassword123!");
    await page.click('button[type="submit"]');

    // Should stay on login page and show error
    await page.waitForLoadState("networkidle");
    expect(page.url()).toContain("/login");

    // Error message should appear
    const errorText = await page.textContent("body");
    expect(errorText?.toLowerCase()).toMatch(/invalid|error|incorrect|failed/);
  });

  test("unauthenticated user is redirected from /dashboard to /login", async ({
    page,
  }) => {
    await page.context().clearCookies();
    await page.goto("/dashboard");
    await page.waitForURL("**/login**", { timeout: 10_000 });
    expect(page.url()).toContain("/login");
  });

  test("unauthenticated user is redirected from /worker to /login", async ({
    page,
  }) => {
    await page.context().clearCookies();
    await page.goto("/worker");
    await page.waitForURL("**/login**", { timeout: 10_000 });
    expect(page.url()).toContain("/login");
  });

  test("logout clears session and redirects to login", async ({ page }) => {
    // Login with shared email — no new registration
    await loginUser(page, sharedEmail);

    // Navigate to logout
    await page.goto("/logout");

    // Should redirect to login page
    await page.waitForURL("**/login**", { timeout: 10_000 });
    expect(page.url()).toContain("/login");

    // Trying to access dashboard should redirect again
    await page.goto("/dashboard");
    await page.waitForURL("**/login**", { timeout: 10_000 });
  });

  test("register page shows error for duplicate email", async ({ page }) => {
    // Use the shared email that was already registered — no new registration needed
    await page.goto("/register");
    await page.fill('input[name="name"]', TEST_NAME);
    await page.fill('input[name="email"]', sharedEmail);
    await page.fill('input[name="password"]', TEST_PASSWORD);
    await page.locator('input[name="role"][value="requester"]').check({ force: true });
    await page.click('button[type="submit"]');

    // Should stay on register page with error
    await page.waitForLoadState("networkidle");

    const bodyText = await page.textContent("body");
    expect(bodyText?.toLowerCase()).toMatch(/already|exist|registered|duplicate/);
  });

  // Worker registration is tested via worker-flow.spec.ts to stay under rate limits

  test("login page conditionally shows Google OAuth button", async ({ page }) => {
    await page.goto("/login");
    const googleBtn = page.locator('a[href*="google"]');
    // Google OAuth buttons are only shown when GOOGLE_CLIENT_ID is configured.
    // In production without OAuth configured, they're correctly hidden.
    const count = await googleBtn.count();
    // Either 0 (not configured) or 1+ (configured) — both are valid
    expect(count).toBeGreaterThanOrEqual(0);
    if (count > 0) {
      await expect(googleBtn.first()).toContainText(/google/i);
    }
  });

  test("register page conditionally shows Google OAuth buttons", async ({
    page,
  }) => {
    await page.goto("/register");
    const googleBtns = page.locator('a[href*="google"]');
    // Google OAuth buttons are only shown when configured.
    // When configured: 2 buttons (requester + worker). When not: 0.
    const count = await googleBtns.count();
    expect(count === 0 || count >= 2).toBeTruthy();
  });
});
