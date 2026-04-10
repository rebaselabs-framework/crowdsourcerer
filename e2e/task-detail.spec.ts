/**
 * Task detail page E2E test — verifies the requester's task detail view
 * renders correctly after creating a task.
 *
 * Creates a task via the API, then navigates to /dashboard/tasks/[id]
 * in the browser and asserts the page shows task metadata correctly.
 *
 * Uses serial mode because tests share state (task ID from creation).
 */
import { test, expect } from "@playwright/test";
import {
  assertNoServerError,
  assertLayoutLoaded,
} from "./helpers";
import { REQUESTER_STATE_PATH } from "./global-setup";

const BASE = "https://crowdsourcerer.rebaselabs.online";

test.describe.configure({ mode: "serial" });

test.describe("Task detail page", () => {
  let token: string;
  let taskId: string;

  test.beforeEach(async ({ context }) => {
    const fs = await import("fs");
    const state = JSON.parse(fs.readFileSync(REQUESTER_STATE_PATH, "utf8"));
    if (state.cookies) {
      await context.addCookies(state.cookies);
    }
  });

  test("create a task via API for detail page tests", async ({ request, context }) => {
    // Get auth token from stored cookies
    const fs = await import("fs");
    const state = JSON.parse(fs.readFileSync(REQUESTER_STATE_PATH, "utf8"));
    const tokenCookie = state.cookies?.find(
      (c: any) => c.name === "cs_token"
    );
    expect(tokenCookie).toBeTruthy();
    token = tokenCookie.value;

    // Create a human task (doesn't depend on RebaseKit being up)
    const resp = await request.post(`${BASE}/v1/tasks`, {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        type: "label_text",
        input: {
          text: "The E2E test framework is working perfectly!",
          categories: ["positive", "negative", "neutral"],
          question: "What is the sentiment of this text?",
        },
        task_instructions: "Pick the most appropriate sentiment label.",
        tags: ["e2e-test", "detail-page"],
      },
    });
    expect(resp.status()).toBe(201);
    const body = await resp.json();
    taskId = body.task_id;
    expect(taskId).toBeTruthy();
  });

  test("task detail page renders without errors", async ({ page }) => {
    test.skip(!taskId, "No task ID from previous test");

    await page.goto(`/dashboard/tasks/${taskId}`);
    await assertNoServerError(page);
    await assertLayoutLoaded(page);

    // Page should not be a 404 or redirect to login
    expect(page.url()).toContain(`/dashboard/tasks/${taskId}`);
  });

  test("detail page shows task type", async ({ page }) => {
    test.skip(!taskId, "No task ID");

    await page.goto(`/dashboard/tasks/${taskId}`);
    await assertNoServerError(page);

    const body = await page.textContent("body");
    // Should show the task type somewhere on the page
    expect(body?.toLowerCase()).toMatch(/label.text|label text/);
  });

  test("detail page shows task status", async ({ page }) => {
    test.skip(!taskId, "No task ID");

    await page.goto(`/dashboard/tasks/${taskId}`);
    await assertNoServerError(page);

    const body = await page.textContent("body");
    // Human tasks start as 'open' or 'pending'
    expect(body?.toLowerCase()).toMatch(/open|pending|queued|completed|failed|running/);
  });

  test("detail page shows task input", async ({ page }) => {
    test.skip(!taskId, "No task ID");

    await page.goto(`/dashboard/tasks/${taskId}`);
    await assertNoServerError(page);

    const body = await page.textContent("body");
    // Should contain part of our task input text
    expect(body).toContain("E2E test framework");
  });

  test("detail page shows task instructions", async ({ page }) => {
    test.skip(!taskId, "No task ID");

    await page.goto(`/dashboard/tasks/${taskId}`);
    await assertNoServerError(page);

    const body = await page.textContent("body");
    expect(body).toContain("sentiment label");
  });

  test("detail page has action buttons", async ({ page }) => {
    test.skip(!taskId, "No task ID");

    await page.goto(`/dashboard/tasks/${taskId}`);
    await assertNoServerError(page);

    const body = await page.textContent("body");
    // Should have at least one of: rerun, duplicate, back link
    expect(body?.toLowerCase()).toMatch(/rerun|duplicate|submit another|back|dashboard/i);
  });

  test("detail page shows task tags", async ({ page }) => {
    test.skip(!taskId, "No task ID");

    await page.goto(`/dashboard/tasks/${taskId}`);
    await assertNoServerError(page);

    const body = await page.textContent("body");
    // Should show at least one of the tags we set
    expect(body).toMatch(/e2e-test|detail-page/);
  });

  test("detail page shows task ID", async ({ page }) => {
    test.skip(!taskId, "No task ID");

    await page.goto(`/dashboard/tasks/${taskId}`);
    await assertNoServerError(page);

    const body = await page.textContent("body");
    // Should show at least the first 8 chars of the task ID
    expect(body).toContain(taskId.slice(0, 8));
  });

  test("non-existent task shows error or 404", async ({ page }) => {
    const fakeId = "00000000-0000-0000-0000-000000000000";
    await page.goto(`/dashboard/tasks/${fakeId}`);

    // Should either show 404 page, error, or redirect
    const body = await page.textContent("body");
    const url = page.url();
    const isError =
      body?.toLowerCase().includes("not found") ||
      body?.toLowerCase().includes("404") ||
      body?.toLowerCase().includes("error") ||
      url.includes("/dashboard/tasks") && !url.includes(fakeId);

    expect(isError).toBeTruthy();
  });

  test("unauthenticated access redirects to login", async ({ browser }) => {
    // Fresh context with no cookies
    const ctx = await browser.newContext({
      baseURL: BASE,
      ignoreHTTPSErrors: true,
    });
    const page = await ctx.newPage();

    await page.goto(`/dashboard/tasks/${taskId || "some-id"}`);

    // Should redirect to login
    await expect(page).toHaveURL(/login/, { timeout: 10_000 });
    await ctx.close();
  });
});
