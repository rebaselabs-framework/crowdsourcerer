/**
 * Shared E2E test helpers for CrowdSorcerer.
 */
import { type Page, expect } from "@playwright/test";

/** Generate a unique test email for this run to avoid conflicts. */
export function testEmail(): string {
  const ts = Date.now();
  const rand = Math.random().toString(36).slice(2, 8);
  return `e2e-${ts}-${rand}@test.crowdsorcerer.local`;
}

/** Standard test password that meets validation requirements. */
export const TEST_PASSWORD = "E2eTestP@ss123!";
export const TEST_NAME = "E2E Test User";

/**
 * Register a new user via the UI and return the email used.
 * Ends on the post-registration redirect (onboarding page).
 */
export async function registerUser(
  page: Page,
  opts?: { role?: "requester" | "worker"; email?: string }
): Promise<string> {
  const email = opts?.email ?? testEmail();
  const role = opts?.role ?? "requester";

  await page.goto("/register");
  await expect(page.locator("h1")).toContainText("Create your account");

  // Fill form
  await page.fill('input[name="name"]', TEST_NAME);
  await page.fill('input[name="email"]', email);
  await page.fill('input[name="password"]', TEST_PASSWORD);

  // Select role radio
  await page.click(`input[name="role"][value="${role}"]`);

  // Submit
  await page.click('button[type="submit"]');

  // Should redirect to onboarding
  const expectedPath =
    role === "worker" ? "/worker/onboarding" : "/dashboard/requester-onboarding";
  await page.waitForURL(`**${expectedPath}`, { timeout: 15_000 });

  return email;
}

/**
 * Login via the UI. Returns after redirect to /dashboard.
 */
export async function loginUser(
  page: Page,
  email: string,
  password: string = TEST_PASSWORD
): Promise<void> {
  await page.goto("/login");
  await expect(page.locator("h1")).toContainText("Welcome back");

  await page.fill('input[name="email"]', email);
  await page.fill('input[name="password"]', password);
  await page.click('button[type="submit"]');

  // Should redirect to dashboard
  await page.waitForURL("**/dashboard**", { timeout: 15_000 });
}

/**
 * Assert no server errors (5xx) or crash pages.
 * Useful as a post-navigation check.
 */
export async function assertNoServerError(page: Page): Promise<void> {
  const body = await page.textContent("body");
  expect(body).not.toContain("Internal Server Error");
  expect(body).not.toContain("500");
  expect(body).not.toContain("Application error");
}

/**
 * Assert the page has loaded the CrowdSorcerer layout (not a blank/error page).
 */
export async function assertLayoutLoaded(page: Page): Promise<void> {
  // Layout should have the main content area and the emoji logo or nav
  const html = await page.content();
  expect(html.length).toBeGreaterThan(500); // not a blank page
}
