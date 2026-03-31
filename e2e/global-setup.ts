/**
 * Global E2E setup — runs once before all test suites.
 *
 * Pre-registers one requester and one worker account and saves their
 * browser storage states to well-known paths. Test suites that just need
 * an authenticated session load these instead of registering their own,
 * which keeps total registrations under the 5/min API rate limit.
 *
 * Accounts created here:
 *   - Requester: test-results/global-requester-state.json
 *   - Worker:    test-results/global-worker-state.json
 */
import { chromium, type FullConfig } from "@playwright/test";
import * as fs from "fs";
import * as path from "path";

const TEST_PASSWORD = "E2eTestP@ss123!";
const TEST_NAME = "E2E Test User";

function testEmail(): string {
  const ts = Date.now();
  const rand = Math.random().toString(36).slice(2, 8);
  return `e2e-${ts}-${rand}@example.com`;
}

export const REQUESTER_STATE_PATH = path.join(
  __dirname,
  "..",
  "test-results",
  "global-requester-state.json"
);
export const WORKER_STATE_PATH = path.join(
  __dirname,
  "..",
  "test-results",
  "global-worker-state.json"
);

async function registerAccount(
  baseURL: string,
  role: "requester" | "worker",
  statePath: string
): Promise<string> {
  const browser = await chromium.launch();
  const context = await browser.newContext({
    baseURL,
    ignoreHTTPSErrors: true,
  });
  const page = await context.newPage();
  const email = testEmail();

  const maxRetries = 2;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    await page.goto("/register");
    await page.fill('input[name="name"]', TEST_NAME);
    await page.fill('input[name="email"]', email);
    await page.fill('input[name="password"]', TEST_PASSWORD);
    await page.locator(`input[name="role"][value="${role}"]`).check({ force: true });
    await page.click('button[type="submit"]');

    const expectedPath =
      role === "worker" ? "/worker/onboarding" : "/dashboard/requester-onboarding";

    try {
      await page.waitForURL(`**${expectedPath}`, { timeout: 15_000 });
      // Success — save state
      await context.storageState({ path: statePath });
      await browser.close();
      return email;
    } catch {
      const body = await page.textContent("body");
      if (body?.toLowerCase().includes("rate limit") && attempt < maxRetries) {
        console.log(`[global-setup] Rate limit on ${role} registration, waiting 65s...`);
        await page.waitForTimeout(65_000);
        continue;
      }
      await browser.close();
      throw new Error(
        `[global-setup] Failed to register ${role}: ${body?.slice(0, 200)}`
      );
    }
  }

  throw new Error(`[global-setup] Exhausted retries for ${role}`);
}

export default async function globalSetup(config: FullConfig) {
  const baseURL =
    process.env.E2E_BASE_URL ?? "https://crowdsourcerer.rebaselabs.online";

  // Ensure output directory exists
  const dir = path.dirname(REQUESTER_STATE_PATH);
  fs.mkdirSync(dir, { recursive: true });

  console.log("[global-setup] Registering shared requester account...");
  const reqEmail = await registerAccount(baseURL, "requester", REQUESTER_STATE_PATH);
  console.log(`[global-setup] Requester registered: ${reqEmail}`);

  console.log("[global-setup] Registering shared worker account...");
  const workerEmail = await registerAccount(baseURL, "worker", WORKER_STATE_PATH);
  console.log(`[global-setup] Worker registered: ${workerEmail}`);
}
