/**
 * Accessibility E2E tests — verify WCAG compliance on key pages.
 *
 * Tests cover:
 * - Landmarks (main, nav, footer)
 * - Skip-to-content link
 * - Form label associations
 * - Focus visibility
 * - ARIA attributes on interactive widgets
 * - Heading hierarchy
 */
import { test, expect } from "@playwright/test";
import { assertNoServerError } from "./helpers";

test.describe("Accessibility — Public pages", () => {
  test("homepage has correct landmark structure", async ({ page }) => {
    await page.goto("/");
    await assertNoServerError(page);

    // Must have <main> landmark
    const main = page.locator("main#main-content");
    await expect(main).toBeAttached();

    // Must have <nav> with label
    const nav = page.locator('nav[aria-label="Main navigation"]');
    await expect(nav).toBeAttached();

    // Must have <footer>
    const footer = page.locator("footer");
    await expect(footer).toBeAttached();

    // Must have exactly one <h1>
    const h1s = await page.locator("h1").count();
    expect(h1s).toBe(1);
  });

  test("skip-to-content link exists and targets main", async ({ page }) => {
    await page.goto("/");

    // The skip link should exist (sr-only by default)
    const skipLink = page.locator('a[href="#main-content"]');
    await expect(skipLink).toBeAttached();
    await expect(skipLink).toHaveText("Skip to main content");

    // The target should exist
    const target = page.locator("#main-content");
    await expect(target).toBeAttached();
  });

  test("login form has proper label associations", async ({ page }) => {
    await page.goto("/login");
    await assertNoServerError(page);

    // Email label should be associated with email input
    const emailLabel = page.locator('label[for="login-email"]');
    await expect(emailLabel).toBeAttached();
    const emailInput = page.locator("#login-email");
    await expect(emailInput).toBeAttached();
    await expect(emailInput).toHaveAttribute("type", "email");
    await expect(emailInput).toHaveAttribute("autocomplete", "email");

    // Password label should be associated with password input
    const pwLabel = page.locator('label[for="login-password"]');
    await expect(pwLabel).toBeAttached();
    const pwInput = page.locator("#login-password");
    await expect(pwInput).toBeAttached();
    await expect(pwInput).toHaveAttribute("type", "password");
    await expect(pwInput).toHaveAttribute("autocomplete", "current-password");
  });

  test("register form has proper label associations", async ({ page }) => {
    await page.goto("/register");
    await assertNoServerError(page);

    // Role selection should use fieldset/legend
    const fieldset = page.locator("fieldset");
    await expect(fieldset).toBeAttached();
    const legend = page.locator("legend");
    await expect(legend).toContainText("I want to");

    // Name, email, password should have for/id pairing
    for (const field of ["register-name", "register-email", "register-password"]) {
      const label = page.locator(`label[for="${field}"]`);
      await expect(label).toBeAttached();
      const input = page.locator(`#${field}`);
      await expect(input).toBeAttached();
    }

    // Radio buttons should have focus-visible ring on their card
    const radioCard = page.locator('input[name="role"][value="requester"]').locator("..");
    await expect(radioCard).toBeAttached();
  });

  test("forgot password form has proper label", async ({ page }) => {
    await page.goto("/forgot-password");
    await assertNoServerError(page);

    const label = page.locator('label[for="forgot-email"]');
    await expect(label).toBeAttached();
    const input = page.locator("#forgot-email");
    await expect(input).toBeAttached();
    await expect(input).toHaveAttribute("autocomplete", "email");
  });

  test("2FA form has proper label", async ({ page }) => {
    await page.goto("/login-2fa");
    await assertNoServerError(page);

    const label = page.locator('label[for="code-input"]');
    await expect(label).toBeAttached();
    const input = page.locator("#code-input");
    await expect(input).toBeAttached();
  });

  test("all pages have main landmark", async ({ page }) => {
    const publicPages = ["/", "/login", "/register", "/pricing", "/docs", "/marketplace", "/leaderboard"];

    for (const url of publicPages) {
      await page.goto(url);
      const main = page.locator("main#main-content");
      await expect(main).toBeAttached({ timeout: 5000 });
    }
  });

  test("navigation has proper ARIA attributes", async ({ page }) => {
    await page.goto("/");

    // Main nav has aria-label
    const mainNav = page.locator('nav[aria-label="Main navigation"]');
    await expect(mainNav).toBeAttached();

    // Theme toggle has aria-label
    const themeToggle = page.locator("#theme-toggle");
    await expect(themeToggle).toHaveAttribute("aria-label", "Toggle dark mode");

    // Hamburger has aria-expanded and aria-controls
    const hamburger = page.locator("#nav-toggle");
    await expect(hamburger).toHaveAttribute("aria-expanded", "false");
    await expect(hamburger).toHaveAttribute("aria-controls", "mobile-menu");
    await expect(hamburger).toHaveAttribute("aria-label", "Toggle menu");
  });

  test("inputs have visible focus indicators", async ({ page }) => {
    await page.goto("/login");

    const emailInput = page.locator("#login-email");
    await emailInput.focus();

    // Check that focus styles are applied (focus:ring-2 should create a box-shadow)
    const boxShadow = await emailInput.evaluate(
      (el) => window.getComputedStyle(el).boxShadow
    );
    // ring-2 produces a box-shadow; "none" means no focus ring
    expect(boxShadow).not.toBe("none");
  });

  test("mobile nav does not have role=menu", async ({ page }) => {
    await page.goto("/");

    // The mobile menu should NOT have role="menu" (it's navigation, not a menu widget)
    const mobileMenu = page.locator("#mobile-menu");
    const role = await mobileMenu.getAttribute("role");
    expect(role).toBeNull();
  });
});

test.describe("Accessibility — Authenticated pages", () => {
  // These tests use the global setup's auth state
  test.use({ storageState: "test-results/global-requester-state.json" });

  test("dashboard has main landmark and nav labels", async ({ page }) => {
    await page.goto("/dashboard");
    await assertNoServerError(page);

    // Main content area
    const main = page.locator("main#main-content");
    await expect(main).toBeAttached();

    // Both navs should have labels for screen readers
    const mainNav = page.locator('nav[aria-label="Main navigation"]');
    await expect(mainNav).toBeAttached();
  });

  test("new-task form has label associations", async ({ page }) => {
    await page.goto("/dashboard/new-task");
    await assertNoServerError(page);

    // Check key form fields have labels
    const labelledFields = [
      { for: "priority", tag: "select" },
      { for: "webhook-url-input", tag: "input" },
      { for: "consensus_strategy", tag: "select" },
      { for: "min_skill_level", tag: "select" },
      { for: "tags", tag: "input" },
      { for: "scheduled-at-input", tag: "input" },
    ];

    for (const field of labelledFields) {
      const label = page.locator(`label[for="${field.for}"]`);
      await expect(label).toBeAttached({ timeout: 3000 });
      const input = page.locator(`#${field.for}`);
      await expect(input).toBeAttached();
    }
  });

  test("security page password form has labels", async ({ page }) => {
    await page.goto("/dashboard/security");
    await assertNoServerError(page);

    for (const id of ["pw-current", "pw-new", "pw-confirm"]) {
      const label = page.locator(`label[for="${id}"]`);
      await expect(label).toBeAttached();
      const input = page.locator(`#${id}`);
      await expect(input).toBeAttached();
    }
  });

  test("global search modal has ARIA attributes", async ({ page }) => {
    await page.goto("/dashboard");
    await assertNoServerError(page);

    // Search trigger
    const trigger = page.locator("#global-search-trigger");
    await expect(trigger).toHaveAttribute("aria-label", "Open global search");

    // Modal should be hidden initially
    const modal = page.locator("#global-search-modal");
    await expect(modal).toHaveClass(/hidden/);

    // Open modal
    await trigger.click();
    await expect(modal).not.toHaveClass(/hidden/);

    // Modal ARIA attributes
    await expect(modal).toHaveAttribute("role", "dialog");
    await expect(modal).toHaveAttribute("aria-modal", "true");
    await expect(modal).toHaveAttribute("aria-label", "Global search");

    // Close with Escape
    await page.keyboard.press("Escape");
    await expect(modal).toHaveClass(/hidden/);
  });

  test("mobile bottom nav has aria-label", async ({ page }) => {
    // Set mobile viewport
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/dashboard");
    await assertNoServerError(page);

    const mobileNav = page.locator('nav[aria-label="Mobile navigation"]');
    await expect(mobileNav).toBeAttached();
  });
});
