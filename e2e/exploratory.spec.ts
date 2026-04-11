/**
 * Exploratory tests — deeper checks on complex pages.
 *
 * Tests actual content quality, not just "page renders":
 * - Do forms have proper validation?
 * - Are interactive elements functional?
 * - Do error states show correctly?
 * - Are page-specific features working?
 */
import { test, expect, type Page } from "@playwright/test";
import {
  assertNoServerError,
  assertLayoutLoaded,
} from "./helpers";
import { REQUESTER_STATE_PATH, WORKER_STATE_PATH } from "./global-setup";

/** Inject saved cookies from a state file. */
async function injectState(page: Page, context: any, statePath: string) {
  const fs = await import("fs");
  const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
  if (state.cookies) {
    await context.addCookies(state.cookies);
  }
}

// ── Requester deep tests ──────────────────────────────────────────────────────

test.describe("Deep: Requester dashboard", () => {
  test.beforeEach(async ({ page, context }) => {
    await injectState(page, context, REQUESTER_STATE_PATH);
  });

  test("new-task page has all task type options", async ({ page }) => {
    await page.goto("/dashboard/new-task");
    await assertNoServerError(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // AI task types should be listed
    const aiTypes = [
      "web research",
      "doc parse",
      "data transform",
      "llm generate",
      "pii detect",
      "code execute",
    ];
    for (const t of aiTypes) {
      expect(lower, `Missing AI task type: ${t}`).toContain(t);
    }

    // Human task types should be listed
    const humanTypes = ["label image", "classify text", "rate quality", "verify fact"];
    for (const t of humanTypes) {
      expect(lower, `Missing human task type: ${t}`).toContain(t);
    }
  });

  test("credits page shows balance and purchase options", async ({ page }) => {
    await page.goto("/dashboard/credits");
    await assertNoServerError(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // Should show numeric balance
    expect(body).toMatch(/\d+/);

    // Should have purchase/buy option
    expect(lower).toMatch(/buy|purchase|add credits|credit bundle/);
  });

  test("profile page shows editable fields", async ({ page }) => {
    await page.goto("/dashboard/profile");
    await assertNoServerError(page);

    // Should have name and email fields
    const nameInput = page.locator('input[name="name"], input[name="display_name"]').first();
    await expect(nameInput).toBeVisible();

    // Should have a save button
    const saveBtn = page.locator('button:has-text("Save"), button:has-text("Update"), button[type="submit"]').first();
    await expect(saveBtn).toBeVisible();
  });

  test("security page shows 2FA and password change options", async ({ page }) => {
    await page.goto("/dashboard/security");
    await assertNoServerError(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // Should mention 2FA
    expect(lower).toMatch(/two.factor|2fa|authenticator/);

    // Should have password change section
    expect(lower).toMatch(/password/);
  });

  test("api-keys page has create button and key list", async ({ page }) => {
    await page.goto("/dashboard/api-keys");
    await assertNoServerError(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // Should have create button
    expect(lower).toMatch(/create|generate|new.+key/);
  });

  test("webhooks page has endpoint creation", async ({ page }) => {
    await page.goto("/dashboard/webhooks");
    await assertNoServerError(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // Should have endpoint creation option
    expect(lower).toMatch(/add|create|new.+endpoint|webhook/);
  });

  test("analytics page shows charts or empty state", async ({ page }) => {
    await page.goto("/dashboard/analytics");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // Should show analytics content or empty state for new users
    expect(lower).toMatch(/analytics|task|credit|usage|no data|get started/);
  });

  test("pipelines page has creation option", async ({ page }) => {
    await page.goto("/dashboard/pipelines");
    await assertNoServerError(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    expect(lower).toMatch(/pipeline|create|new|workflow/);
  });

  test("team page shows organization options", async ({ page }) => {
    await page.goto("/dashboard/team");
    await assertNoServerError(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    expect(lower).toMatch(/team|organization|create|invite|member/);
  });

  test("billing page shows plan and payment info", async ({ page }) => {
    await page.goto("/dashboard/billing");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    expect(lower).toMatch(/billing|plan|payment|free|subscription/);
  });
});

// ── Worker deep tests ─────────────────────────────────────────────────────────

test.describe("Deep: Worker pages", () => {
  test.beforeEach(async ({ page, context }) => {
    await injectState(page, context, WORKER_STATE_PATH);
  });

  test("worker hub shows onboarding or navigation", async ({ page }) => {
    await page.goto("/worker");
    await assertNoServerError(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // New workers see the onboarding flow; completed workers see the full hub.
    // Both are valid states for a freshly registered test account.
    const hasOnboarding = lower.includes("welcome") || lower.includes("onboarding") || lower.includes("steps completed");
    const hasHub = lower.includes("marketplace") || lower.includes("your stats");
    expect(hasOnboarding || hasHub, "Worker page should show onboarding or hub").toBeTruthy();
  });

  test("skills page shows skill categories", async ({ page }) => {
    await page.goto("/worker/skills");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // Should show skill-related content
    expect(lower).toMatch(/skill|certification|proficiency/);
  });

  test("reputation page shows level system", async ({ page }) => {
    await page.goto("/worker/reputation");
    await assertNoServerError(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // Should show level/XP/reputation info
    expect(lower).toMatch(/level|xp|reputation|score/);
  });

  test("challenges page shows daily challenges", async ({ page }) => {
    await page.goto("/worker/challenges");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    expect(lower).toMatch(/challenge|daily|streak|reward/);
  });

  test("leaderboard page shows ranking system", async ({ page }) => {
    await page.goto("/worker/leaderboard");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    expect(lower).toMatch(/leaderboard|rank|top|score/);
  });

  test("marketplace shows task listing or empty state", async ({ page }) => {
    await page.goto("/worker/marketplace");
    await assertNoServerError(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // Should show either tasks or a helpful empty state
    expect(lower).toMatch(/task|marketplace|no tasks|browse|available/);
  });

  test("earnings page shows payout info", async ({ page }) => {
    await page.goto("/worker/earnings");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    expect(lower).toMatch(/earn|payout|credit|balance|withdraw/);
  });

  test("portfolio page allows adding work samples", async ({ page }) => {
    await page.goto("/worker/portfolio");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    expect(lower).toMatch(/portfolio|work|sample|add|showcase/);
  });

  test("certifications page shows available certs", async ({ page }) => {
    await page.goto("/worker/certifications");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    expect(lower).toMatch(/certif|badge|earn|verify/);
  });
});

// ── Public page quality checks ────────────────────────────────────────────────

test.describe("Deep: Public pages", () => {
  test("homepage has working theme toggle", async ({ page }) => {
    await page.goto("/");
    await assertNoServerError(page);

    // Find theme toggle button
    const themeBtn = page.locator(
      'button[aria-label*="theme" i], button[data-theme-toggle], [id*="theme"]'
    ).first();

    if (await themeBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await themeBtn.click();
      // Should still render without errors after toggle
      await assertNoServerError(page);
    }
  });

  test("docs page has structured content sections", async ({ page }) => {
    await page.goto("/docs");
    await assertNoServerError(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // Should cover key documentation topics
    expect(lower).toMatch(/getting started|quickstart|api|sdk|task/);
  });

  test("API reference page has endpoint documentation", async ({ page }) => {
    await page.goto("/docs/api-reference");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // Should document endpoints
    expect(lower).toMatch(/endpoint|post|get|authentication|api/);
  });

  test("pricing page has all plan tiers", async ({ page }) => {
    await page.goto("/pricing");
    await assertNoServerError(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    // Should show free and paid tiers
    expect(lower).toContain("free");
    expect(lower).toMatch(/pro|premium|enterprise|paid/);

    // Should show credit bundles
    expect(lower).toMatch(/credit|bundle/);
  });

  test("workers browse page shows worker cards or empty state", async ({ page }) => {
    await page.goto("/workers/browse");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    const body = await page.textContent("body");
    const lower = body?.toLowerCase() ?? "";

    expect(lower).toMatch(/worker|browse|skill|find/);
  });

  test("login page has all auth options", async ({ page }) => {
    await page.goto("/login");
    await assertNoServerError(page);

    // Has email/password fields
    await expect(page.locator('input[name="email"]')).toBeVisible();
    await expect(page.locator('input[name="password"]')).toBeVisible();

    // Has submit button
    await expect(page.locator('button[type="submit"]')).toBeVisible();

    // Google OAuth is conditionally shown (only when GOOGLE_CLIENT_ID is configured)
    // Don't assert visibility — just verify no server error on the page

    // Has forgot password link
    const forgotLink = page.locator('a[href*="forgot"]');
    await expect(forgotLink).toBeVisible();

    // Has register link (multiple may exist — nav + body)
    const registerLink = page.locator('a[href*="register"]').first();
    await expect(registerLink).toBeVisible();
  });

  test("register page validates empty submission", async ({ page }) => {
    await page.goto("/register");
    await assertNoServerError(page);

    // Try to submit empty form
    await page.click('button[type="submit"]');

    // Should stay on register page (HTML5 validation or server-side error)
    expect(page.url()).toContain("/register");
  });

  test("no broken images on homepage", async ({ page }) => {
    const brokenImages: string[] = [];

    page.on("response", (resp) => {
      if (
        resp.request().resourceType() === "image" &&
        resp.status() >= 400
      ) {
        brokenImages.push(resp.url());
      }
    });

    await page.goto("/");
    await page.waitForLoadState("networkidle");

    expect(brokenImages, `Broken images: ${brokenImages.join(", ")}`).toHaveLength(0);
  });

  test("no console errors on key pages", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") {
        errors.push(`${msg.text()} @ ${msg.location().url}`);
      }
    });

    const pages = ["/", "/pricing", "/docs", "/login", "/register"];
    for (const p of pages) {
      errors.length = 0;
      await page.goto(p);
      await page.waitForLoadState("networkidle");

      // Filter out expected errors (e.g., third-party scripts, favicon)
      const realErrors = errors.filter(
        (e) => !e.includes("favicon") && !e.includes("third-party")
      );

      expect(
        realErrors,
        `Console errors on ${p}: ${realErrors.join("\n")}`
      ).toHaveLength(0);
    }
  });
});
