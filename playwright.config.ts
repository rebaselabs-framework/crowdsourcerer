import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E config for CrowdSorcerer.
 *
 * TARGET: The live deployment at crowdsourcerer.rebaselabs.online.
 * Override with E2E_BASE_URL env var for local dev or custom domains.
 *
 * Usage:
 *   npx playwright test                    # run all E2E tests
 *   npx playwright test e2e/smoke.spec.ts  # run one suite
 *   E2E_BASE_URL=http://localhost:4321 npx playwright test  # local dev
 */
export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: false, // sequential — tests may share state (registered user)
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  timeout: 30_000,

  use: {
    baseURL:
      process.env.E2E_BASE_URL ??
      "https://crowdsourcerer.rebaselabs.online",
    ignoreHTTPSErrors: true, // allow self-signed certs in dev/staging
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    headless: true,
    viewport: { width: 1280, height: 720 },
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
