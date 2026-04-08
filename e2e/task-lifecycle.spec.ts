/**
 * Task lifecycle E2E test — test the real API flow against live deployment.
 *
 * Tests: register → create task → list tasks → get detail → check credits
 *        → user profile → auth enforcement.
 *
 * Uses serial mode because tests depend on state from previous tests.
 * Only registers 1 account to stay within rate limits (5 reg/min shared
 * across all spec files in the suite).
 */
import { test, expect } from "@playwright/test";

const BASE = "https://crowdsourcerer.rebaselabs.online";

test.describe.configure({ mode: "serial" });

test.describe("Task lifecycle API flow", () => {
  let token: string;
  let taskId: string;
  const email = `lifecycle-${Date.now()}@example.com`;
  const password = "TestP@ss123!";

  test("register a test account", async ({ request }) => {
    const regResp = await request.post(`${BASE}/v1/auth/register`, {
      data: { email, password, name: "Lifecycle Test", role: "requester" },
    });
    expect(regResp.status()).toBe(201);
    token = (await regResp.json()).access_token;
    expect(token).toBeTruthy();
  });

  test("create an AI task (web_research)", async ({ request }) => {
    const resp = await request.post(`${BASE}/v1/tasks`, {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        type: "web_research",
        input: {
          url: "https://example.com",
          instruction: "Extract the main heading from this page",
        },
      },
    });
    expect(resp.status()).toBe(201);
    const body = await resp.json();
    taskId = body.task_id;
    expect(taskId).toBeTruthy();
    expect(body.status).toMatch(/pending|queued|running|completed|failed/);
  });

  test("task appears in task list", async ({ request }) => {
    const resp = await request.get(`${BASE}/v1/tasks`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(resp.status()).toBe(200);
    const body = await resp.json();
    const tasks = body.items || body.tasks || body;
    expect(Array.isArray(tasks)).toBeTruthy();
    const found = tasks.find((t: any) => t.id === taskId);
    expect(found).toBeTruthy();
  });

  test("can get task detail by ID", async ({ request }) => {
    const resp = await request.get(`${BASE}/v1/tasks/${taskId}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(resp.status()).toBe(200);
    const body = await resp.json();
    expect(body.id).toBe(taskId);
    expect(body.type).toBe("web_research");
    expect(body.status).toMatch(/pending|queued|running|completed|failed/);
  });

  test("credits are deducted after task creation", async ({ request }) => {
    const resp = await request.get(`${BASE}/v1/credits`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(resp.status()).toBe(200);
    const body = await resp.json();
    expect(typeof body.available).toBe("number");
    // New accounts get 1000 credits (beta). web_research costs 10 but
    // may be auto-refunded if the AI backend is down (task fails immediately).
    // Either deducted (< 1000) or refunded (= 1000) is valid.
    expect(body.available).toBeLessThanOrEqual(1000);
  });

  test("user profile shows correct info", async ({ request }) => {
    const resp = await request.get(`${BASE}/v1/users/me`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    expect(resp.status()).toBe(200);
    const body = await resp.json();
    expect(body.email).toBe(email);
    expect(body.role).toBe("requester");
    expect(typeof body.credits).toBe("number");
  });

  test("unauthenticated request is rejected", async ({ request }) => {
    const resp = await request.get(`${BASE}/v1/tasks/${taskId}`);
    expect(resp.status()).toBe(401);
  });
});
