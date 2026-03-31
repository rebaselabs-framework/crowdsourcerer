/**
 * Auth flow tests — registration, login, logout, auth redirects.
 *
 * These tests interact with the LIVE deployed app and create real users.
 * We use unique emails per run to avoid conflicts.
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
  let registeredEmail: string;

  test("register a new requester account", async ({ page }) => {
    registeredEmail = await registerUser(page, { role: "requester" });

    // Should now be on the onboarding page
    expect(page.url()).toContain("/dashboard/requester-onboarding");
    await assertNoServerError(page);
  });

  test("login with the newly registered account", async ({ page }) => {
    // First register to get a valid email
    const email = await registerUser(page, { role: "requester" });

    // Logout by clearing cookies
    await page.context().clearCookies();

    // Now login
    await loginUser(page, email);

    // Should be on dashboard
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
    // Clear any existing auth
    await page.context().clearCookies();

    await page.goto("/dashboard");
    // Should redirect to login
    await page.waitForURL("**/login**", { timeout: 10_000 });
    expect(page.url()).toContain("/login");
  });

  test("unauthenticated user is redirected from /worker to /login", async ({
    page,
  }) => {
    await page.context().clearCookies();

    await page.goto("/worker");
    // Worker pages should also redirect to login
    await page.waitForURL("**/login**", { timeout: 10_000 });
    expect(page.url()).toContain("/login");
  });

  test("logout clears session and redirects to login", async ({ page }) => {
    // Register + login
    const email = await registerUser(page, { role: "requester" });
    await page.context().clearCookies();
    await loginUser(page, email);

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
    // Register once
    const email = await registerUser(page, { role: "requester" });

    // Clear cookies and try to register again with same email
    await page.context().clearCookies();
    await page.goto("/register");
    await page.fill('input[name="name"]', TEST_NAME);
    await page.fill('input[name="email"]', email);
    await page.fill('input[name="password"]', TEST_PASSWORD);
    await page.click('input[name="role"][value="requester"]');
    await page.click('button[type="submit"]');

    // Should stay on register page with error
    await page.waitForLoadState("networkidle");

    const bodyText = await page.textContent("body");
    expect(bodyText?.toLowerCase()).toMatch(/already|exist|registered|duplicate/);
  });

  test("register a worker account routes to worker onboarding", async ({
    page,
  }) => {
    await registerUser(page, { role: "worker" });
    expect(page.url()).toContain("/worker/onboarding");
    await assertNoServerError(page);
  });

  test("login page has Google OAuth button", async ({ page }) => {
    await page.goto("/login");
    const googleBtn = page.locator('a[href*="google"]');
    await expect(googleBtn).toBeVisible();
    await expect(googleBtn).toContainText(/google/i);
  });

  test("register page has Google OAuth buttons for both roles", async ({
    page,
  }) => {
    await page.goto("/register");
    const googleBtns = page.locator('a[href*="google"]');
    // Should have at least 2 (requester + worker)
    expect(await googleBtns.count()).toBeGreaterThanOrEqual(2);
  });
});
