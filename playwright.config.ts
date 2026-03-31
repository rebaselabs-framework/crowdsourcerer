import { defineConfig, devices } from "@playwright/test";

/**
 * Playwright E2E config for CrowdSorcerer.
 *
 * TARGET: The live Coolify deployment (sslip.io auto-domain).
 * Override with E2E_BASE_URL env var for local dev or custom domains.
 *
 * Usage:
 *   npx playwright test                    # run all E2E tests
 *   npx playwright test e2e/smoke.spec.ts  # run one suite
 *   E2E_BASE_URL=http://localhost:4321 npx playwright test  # local dev
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false, // sequential — tests may share state (registered user)
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [["list"], ["html", { open: "never" }]],
  timeout: 30_000,

  use: {
    baseURL:
      process.env.E2E_BASE_URL ??
      "https://bvbzhp7j15a7nxiqf9vm53ey.10.0.1.1.sslip.io",
    ignoreHTTPSErrors: true, // sslip.io uses self-signed certs
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
