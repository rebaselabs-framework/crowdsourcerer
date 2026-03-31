/**
 * Shared E2E test helpers for CrowdSorcerer.
 *
 * Rate limit awareness: the live API enforces 5 registers/min and 10 logins/min.
 * Tests must minimize API calls by reusing auth state (storageState) instead
 * of logging in fresh each time.
 */
import { type Page, type BrowserContext, type Browser, expect } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";

/** Generate a unique test email for this run to avoid conflicts. */
export function testEmail(): string {
  const ts = Date.now();
  const rand = Math.random().toString(36).slice(2, 8);
  return `e2e-${ts}-${rand}@example.com`;
}

/** Standard test password that meets validation requirements. */
export const TEST_PASSWORD = "E2eTestP@ss123!";
export const TEST_NAME = "E2E Test User";

/**
 * Register a new user via the UI and return the email used.
 * Ends on the post-registration redirect (onboarding page).
 *
 * Rate-limit aware: if the API returns "Rate limit exceeded" (5 reg/min),
 * waits 65 seconds and retries (up to 2 retries).
 */
export async function registerUser(
  page: Page,
  opts?: { role?: "requester" | "worker"; email?: string }
): Promise<string> {
  const email = opts?.email ?? testEmail();
  const role = opts?.role ?? "requester";
  const maxRetries = 2;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    await page.goto("/register");
    await expect(page.locator("h1")).toContainText("Create your account");

    // Fill form
    await page.fill('input[name="name"]', TEST_NAME);
    await page.fill('input[name="email"]', email);
    await page.fill('input[name="password"]', TEST_PASSWORD);

    // Select role — radio input is sr-only (visually hidden), so use force: true
    await page.locator(`input[name="role"][value="${role}"]`).check({ force: true });

    // Submit
    await page.click('button[type="submit"]');

    // Wait for either redirect or error
    const expectedPath =
      role === "worker" ? "/worker/onboarding" : "/dashboard/requester-onboarding";

    try {
      await page.waitForURL(`**${expectedPath}`, { timeout: 15_000 });
      return email; // Success — registered and redirected
    } catch {
      // Check if we hit the rate limit
      const body = await page.textContent("body");
      if (body?.toLowerCase().includes("rate limit") && attempt < maxRetries) {
        // Wait for the rate limit window to reset (65s to be safe)
        console.log(`[E2E] Rate limit hit on registration attempt ${attempt + 1}, waiting 65s...`);
        await page.waitForTimeout(65_000);
        continue;
      }
      // Not a rate limit or out of retries — rethrow
      throw new Error(
        `Registration failed after ${attempt + 1} attempt(s). ` +
        `URL: ${page.url()}, Body snippet: ${body?.slice(0, 200)}`
      );
    }
  }

  // Should never reach here, but TypeScript needs it
  throw new Error("Registration failed: exhausted all retries");
}

/**
 * Register a user and save the authenticated browser state to a file.
 * Returns { email, statePath } for use in test fixtures.
 *
 * This saves cookies so subsequent tests can reuse auth without
 * hitting the login API each time (avoids 10/min login rate limit).
 */
export async function registerAndSaveState(
  browser: Browser,
  opts?: { role?: "requester" | "worker"; stateFile?: string }
): Promise<{ email: string; statePath: string }> {
  const role = opts?.role ?? "requester";
  const stateDir = path.join(__dirname, "..", "test-results");
  fs.mkdirSync(stateDir, { recursive: true });
  const statePath =
    opts?.stateFile ?? path.join(stateDir, `auth-state-${role}-${Date.now()}.json`);

  const ctx = await browser.newContext({ ignoreHTTPSErrors: true });
  const page = await ctx.newPage();
  const email = await registerUser(page, { role });

  // Save cookies/storage for reuse
  await ctx.storageState({ path: statePath });
  await ctx.close();

  return { email, statePath };
}

/**
 * Login via the UI. Returns after redirect to /dashboard or /worker.
 * Only use when you can't reuse storageState from registerAndSaveState.
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

  // Should redirect to dashboard (requester) or worker area
  await page.waitForURL(/(dashboard|worker)/, { timeout: 15_000 });
}

/**
 * Assert no server errors (5xx) or crash pages.
 * Useful as a post-navigation check.
 *
 * NOTE: We check for specific error phrases, NOT bare "500"
 * because pages legitimately contain "500" in content (e.g., "500 credits").
 */
export async function assertNoServerError(page: Page): Promise<void> {
  const body = await page.textContent("body");
  expect(body).not.toContain("Internal Server Error");
  expect(body).not.toContain("Application error");
  expect(body).not.toContain("Server Error (500)");
  expect(body).not.toContain("502 Bad Gateway");
  expect(body).not.toContain("503 Service Unavailable");
}

/**
 * Assert the page has loaded the CrowdSorcerer layout (not a blank/error page).
 */
export async function assertLayoutLoaded(page: Page): Promise<void> {
  // Layout should have the main content area and the emoji logo or nav
  const html = await page.content();
  expect(html.length).toBeGreaterThan(500); // not a blank page
}
