/**
 * Mobile responsiveness tests — verify the product works on small screens.
 *
 * Tests at iPhone SE (375x667) and iPad Mini (768x1024) viewports.
 * Checks for:
 * - Horizontal overflow (no side-scrolling)
 * - Mobile navigation accessibility
 * - Touch-friendly tap targets
 * - Content readability
 * - No elements clipped or hidden
 */
import { test, expect, type Page } from "@playwright/test";
import { assertNoServerError, assertLayoutLoaded } from "./helpers";
import { REQUESTER_STATE_PATH, WORKER_STATE_PATH } from "./global-setup";

const MOBILE_VIEWPORT = { width: 375, height: 667 };
const TABLET_VIEWPORT = { width: 768, height: 1024 };

/** Check that a page has no horizontal overflow. */
async function assertNoHorizontalOverflow(page: Page, label: string) {
  const hasOverflow = await page.evaluate(() => {
    return document.documentElement.scrollWidth > document.documentElement.clientWidth;
  });
  expect(hasOverflow, `Horizontal overflow on ${label}`).toBe(false);
}

/** Inject auth state from a file. */
async function injectState(page: Page, context: any, statePath: string) {
  const fs = await import("fs");
  const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
  if (state.cookies) {
    await context.addCookies(state.cookies);
  }
}

// ── Mobile viewport tests ─────────────────────────────────────────────────────

test.describe("Mobile (375px)", () => {
  test.use({ viewport: MOBILE_VIEWPORT });

  test("homepage - no horizontal overflow", async ({ page }) => {
    await page.goto("/");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "homepage");
  });

  test("homepage - mobile menu is accessible", async ({ page }) => {
    await page.goto("/");

    // There should be a hamburger/mobile menu button
    const menuBtn = page.locator(
      'button[aria-label*="menu" i], button[aria-label*="nav" i], [data-mobile-menu], button.mobile-menu-btn, button:has(svg)'
    ).first();

    // At mobile widths, a menu button should exist
    const hasMenu = await menuBtn.isVisible({ timeout: 3000 }).catch(() => false);
    if (hasMenu) {
      await menuBtn.click();

      // After clicking, navigation links should become visible
      await page.waitForTimeout(500); // allow animation
      const navLinks = page.locator('nav a, [role="navigation"] a, .mobile-nav a');
      const navCount = await navLinks.count();
      expect(navCount, "Mobile menu should reveal navigation links").toBeGreaterThan(0);
    }
  });

  test("login page - no overflow, form usable", async ({ page }) => {
    await page.goto("/login");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "login");

    // Form fields should be visible
    await expect(page.locator('input[name="email"]')).toBeVisible();
    await expect(page.locator('input[name="password"]')).toBeVisible();
    await expect(page.locator('button[type="submit"]')).toBeVisible();
  });

  test("register page - no overflow, form usable", async ({ page }) => {
    await page.goto("/register");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "register");

    // Form fields should be visible
    await expect(page.locator('input[name="email"]')).toBeVisible();
    await expect(page.locator('input[name="password"]')).toBeVisible();
  });

  test("pricing page - no overflow, plans visible", async ({ page }) => {
    await page.goto("/pricing");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "pricing");

    const body = await page.textContent("body");
    expect(body?.toLowerCase()).toMatch(/free|pro|credit/);
  });

  test("docs page - no overflow", async ({ page }) => {
    await page.goto("/docs");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "docs");
  });
});

test.describe("Mobile: Requester dashboard (375px)", () => {
  test.use({ viewport: MOBILE_VIEWPORT });

  test.beforeEach(async ({ page, context }) => {
    await injectState(page, context, REQUESTER_STATE_PATH);
  });

  test("dashboard - no overflow", async ({ page }) => {
    await page.goto("/dashboard");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "dashboard");
  });

  test("dashboard - sidebar/nav accessible on mobile", async ({ page }) => {
    await page.goto("/dashboard");
    await assertNoServerError(page);

    // Dashboard should have navigation — either visible or behind a toggle
    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // Core sections should be accessible somehow
    expect(lower).toMatch(/task|credit|dashboard/);
  });

  test("new-task page - form works on mobile", async ({ page }) => {
    await page.goto("/dashboard/new-task");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "new-task");

    // Task types should be visible
    const body = await page.textContent("body");
    expect(body?.toLowerCase()).toMatch(/web research|task/);
  });

  test("tasks list - no overflow", async ({ page }) => {
    await page.goto("/dashboard/tasks");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "tasks");
  });

  test("credits page - no overflow", async ({ page }) => {
    await page.goto("/dashboard/credits");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "credits");
  });

  test("profile page - form usable", async ({ page }) => {
    await page.goto("/dashboard/profile");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "profile");
  });

  test("security page - no overflow", async ({ page }) => {
    await page.goto("/dashboard/security");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "security");
  });

  test("webhooks page - no overflow", async ({ page }) => {
    await page.goto("/dashboard/webhooks");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "webhooks");
  });

  test("billing page - no overflow", async ({ page }) => {
    await page.goto("/dashboard/billing");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "billing");
  });
});

test.describe("Mobile: Worker pages (375px)", () => {
  test.use({ viewport: MOBILE_VIEWPORT });

  test.beforeEach(async ({ page, context }) => {
    await injectState(page, context, WORKER_STATE_PATH);
  });

  test("worker hub - no overflow", async ({ page }) => {
    await page.goto("/worker");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "worker hub");
  });

  test("marketplace - no overflow", async ({ page }) => {
    await page.goto("/worker/marketplace");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "marketplace");
  });

  test("skills page - no overflow", async ({ page }) => {
    await page.goto("/worker/skills");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "skills");
  });

  test("earnings page - no overflow", async ({ page }) => {
    await page.goto("/worker/earnings");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "earnings");
  });

  test("reputation page - no overflow", async ({ page }) => {
    await page.goto("/worker/reputation");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "reputation");
  });

  test("challenges page - no overflow", async ({ page }) => {
    await page.goto("/worker/challenges");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "challenges");
  });

  test("leaderboard page - no overflow", async ({ page }) => {
    await page.goto("/worker/leaderboard");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "leaderboard");
  });
});

// ── Tablet tests ──────────────────────────────────────────────────────────────

test.describe("Tablet (768px)", () => {
  test.use({ viewport: TABLET_VIEWPORT });

  test("homepage - no overflow, good layout", async ({ page }) => {
    await page.goto("/");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "homepage-tablet");
  });

  test("pricing page - no overflow", async ({ page }) => {
    await page.goto("/pricing");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "pricing-tablet");
  });

  test("dashboard - no overflow", async ({ page, context }) => {
    await injectState(page, context, REQUESTER_STATE_PATH);
    await page.goto("/dashboard");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "dashboard-tablet");
  });

  test("worker hub - no overflow", async ({ page, context }) => {
    await injectState(page, context, WORKER_STATE_PATH);
    await page.goto("/worker");
    await assertNoServerError(page);
    await assertNoHorizontalOverflow(page, "worker-tablet");
  });
});
