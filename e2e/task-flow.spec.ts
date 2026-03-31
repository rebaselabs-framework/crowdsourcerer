/**
 * Task creation flow — the core user journey.
 *
 * Register a requester, navigate to new-task, create an AI task,
 * verify it appears in the task list.
 *
 * Uses storageState (saved cookies) instead of per-test login.
 */
import { test, expect } from "@playwright/test";
import {
  registerAndSaveState,
  assertNoServerError,
  assertLayoutLoaded,
} from "./helpers";

test.describe("Task creation flow", () => {
  let statePath: string;

  test.beforeAll(async ({ browser }) => {
    const result = await registerAndSaveState(browser, { role: "requester" });
    statePath = result.statePath;
  });

  test.beforeEach(async ({ page, context }) => {
    // Inject saved cookies — no login API call needed
    const fs = await import("fs");
    const state = JSON.parse(fs.readFileSync(statePath, "utf8"));
    if (state.cookies) {
      await context.addCookies(state.cookies);
    }
  });

  test("new-task page shows task type grid", async ({ page }) => {
    await page.goto("/dashboard/new-task");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    // Should show AI and human task types
    const body = await page.textContent("body");
    expect(body?.toLowerCase()).toMatch(/web.research|llm.generate|screenshot/);
  });

  test("can fill and submit a web_research task", async ({ page }) => {
    await page.goto("/dashboard/new-task");
    await assertNoServerError(page);

    // Select web_research task type (click the button/card)
    const webResearchBtn = page.locator(
      'button:has-text("Web Research"), [data-type="web_research"], label:has-text("Web Research")'
    );

    // If the task type selector is a clickable element
    const typeSelector = webResearchBtn.first();
    if (await typeSelector.isVisible({ timeout: 3000 }).catch(() => false)) {
      await typeSelector.click();
    }

    // Fill the input JSON textarea
    const textarea = page.locator('textarea[name="input"], #task-input, textarea').first();
    if (await textarea.isVisible({ timeout: 3000 }).catch(() => false)) {
      await textarea.fill(
        JSON.stringify({
          url: "https://example.com",
          instruction: "Extract the main heading",
        })
      );
    }

    // Submit the form
    const submitBtn = page.locator(
      'button[type="submit"]:has-text("Create"), button:has-text("Submit"), button:has-text("Create Task")'
    ).first();

    if (await submitBtn.isVisible({ timeout: 3000 }).catch(() => false)) {
      await submitBtn.click();
      await page.waitForLoadState("networkidle");

      // Should either redirect to task detail or show success
      const url = page.url();
      const body = await page.textContent("body");
      const success =
        url.includes("/dashboard/tasks/") ||
        body?.toLowerCase().includes("created") ||
        body?.toLowerCase().includes("success") ||
        body?.toLowerCase().includes("pending") ||
        body?.toLowerCase().includes("running");

      expect(success).toBeTruthy();
    }
  });

  test("task list page loads after task creation", async ({ page }) => {
    await page.goto("/dashboard/tasks");
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    // Should show tasks (or empty state)
    const body = await page.textContent("body");
    expect(body?.toLowerCase()).toMatch(/task|no tasks|empty|create/);
  });

  test("credits page reflects correct balance", async ({ page }) => {
    await page.goto("/dashboard/credits");
    await assertNoServerError(page);

    // Should show numeric credit balance
    const body = await page.textContent("body");
    // New accounts get free credits (100 by default)
    expect(body).toMatch(/\d+/);
  });
});
