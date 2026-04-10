/**
 * Comprehensive tests for the CrowdSorcerer TypeScript SDK.
 *
 * Uses vitest + a fetch mock to test all client methods,
 * error handling, retry logic, and webhook verification.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { CrowdSorcerer, verifyWebhook } from "../client";
import {
  CrowdSorcererError,
  AuthError,
  RateLimitError,
  InsufficientCreditsError,
} from "../errors";

// ─── Test Helpers ────────────────────────────────────────────────────────────

const BASE_URL = "https://test.crowdsourcerer.local";
const API_KEY = "cs_test_key_abc123";

function makeClient(opts: Partial<ConstructorParameters<typeof CrowdSorcerer>[0]> = {}) {
  return new CrowdSorcerer({
    apiKey: API_KEY,
    baseUrl: BASE_URL,
    ...opts,
  });
}

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...headers },
  });
}

function noContentResponse() {
  return new Response(null, { status: 204 });
}

const TASK_FIXTURE = {
  id: "00000000-0000-0000-0000-000000000001",
  type: "llm_generate",
  status: "completed",
  priority: "normal",
  execution_mode: "ai",
  input: { messages: [{ role: "user", content: "Hello" }] },
  output: { raw: "Hi there!", summary: "Greeting response" },
  created_at: "2026-01-01T00:00:00Z",
  credits_used: 1,
};

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ─── Constructor ─────────────────────────────────────────────────────────────

describe("CrowdSorcerer constructor", () => {
  it("requires an apiKey", () => {
    expect(() => new CrowdSorcerer({ apiKey: "" })).toThrow(AuthError);
  });

  it("uses default base URL when not provided", () => {
    const client = new CrowdSorcerer({ apiKey: API_KEY });
    // Trigger a request and check the URL
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "me" }));
    client.getMe();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("crowdsourcerer.rebaselabs.online"),
      expect.anything()
    );
  });

  it("strips trailing slash from baseUrl", () => {
    const client = makeClient({ baseUrl: "https://example.com/" });
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "me" }));
    client.getMe();
    expect(fetchMock).toHaveBeenCalledWith(
      "https://example.com/v1/users/me",
      expect.anything()
    );
  });
});

// ─── HTTP Layer / Auth Headers ───────────────────────────────────────────────

describe("HTTP layer", () => {
  it("sends Authorization header with Bearer token", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "user-1" }));
    const client = makeClient();
    await client.getMe();

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers.Authorization).toBe(`Bearer ${API_KEY}`);
  });

  it("sends X-Client header with SDK version", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "user-1" }));
    const client = makeClient();
    await client.getMe();

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers["X-Client"]).toBe("crowdsourcerer-sdk/1.0.0");
  });

  it("sends Content-Type: application/json", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ id: "user-1" }));
    const client = makeClient();
    await client.getMe();

    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers["Content-Type"]).toBe("application/json");
  });
});

// ─── Error Handling ──────────────────────────────────────────────────────────

describe("error handling", () => {
  it("throws AuthError on 401", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "Unauthorized" }, 401));
    const client = makeClient();
    await expect(client.getMe()).rejects.toThrow(AuthError);
  });

  it("AuthError includes requestId from header", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "Unauthorized" }, 401, { "x-request-id": "req-123" })
    );
    const client = makeClient();
    try {
      await client.getMe();
      expect.unreachable("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(AuthError);
      expect((e as AuthError).requestId).toBe("req-123");
    }
  });

  it("throws RateLimitError on 429 with retry-after", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "Too many requests" }, 429, { "retry-after": "30" })
    );
    // maxRetries: 0 — we want the error, not the retry behaviour here.
    const client = makeClient({ maxRetries: 0 });
    try {
      await client.getMe();
      expect.unreachable("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(RateLimitError);
      expect((e as RateLimitError).retryAfter).toBe(30);
    }
  });

  it("throws InsufficientCreditsError on 402", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ required: 10, available: 3 }, 402)
    );
    const client = makeClient();
    await expect(
      client.submitTask({ type: "web_research", input: { url: "https://example.com" } })
    ).rejects.toThrow(InsufficientCreditsError);
  });

  it("InsufficientCreditsError message includes amounts", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ required: 10, available: 3 }, 402)
    );
    const client = makeClient();
    try {
      await client.submitTask({ type: "web_research", input: { url: "https://example.com" } });
      expect.unreachable("should have thrown");
    } catch (e) {
      expect((e as InsufficientCreditsError).message).toContain("10");
      expect((e as InsufficientCreditsError).message).toContain("3");
    }
  });

  it("throws CrowdSorcererError on 404", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: "not_found", message: "Task not found" }, 404)
    );
    const client = makeClient();
    await expect(client.getTask("nonexistent")).rejects.toThrow(CrowdSorcererError);
  });

  it("throws CrowdSorcererError on 500 with detail from body", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: "internal", message: "Server error" }, 500)
    );
    const client = makeClient({ maxRetries: 0 });
    try {
      await client.getMe();
      expect.unreachable("should have thrown");
    } catch (e) {
      expect(e).toBeInstanceOf(CrowdSorcererError);
      expect((e as CrowdSorcererError).status).toBe(500);
    }
  });

  it("handles non-JSON error responses gracefully", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("Bad Gateway", { status: 502 })
    );
    const client = makeClient({ maxRetries: 0 });
    await expect(client.getMe()).rejects.toThrow(CrowdSorcererError);
  });

  it("handles 204 No Content responses", async () => {
    fetchMock.mockResolvedValueOnce(noContentResponse());
    const client = makeClient();
    // cancelTask expects 204
    const result = await client.cancelTask("task-1");
    expect(result).toBeUndefined();
  });
});

// ─── Retries ─────────────────────────────────────────────────────────────────

describe("retry logic", () => {
  // Keep backoff effectively instant so tests stay fast. The full-jitter
  // formula picks in [0, window); base=1 + cap=1 → max 1ms per sleep.
  const RETRY_OPTS = { retryBaseDelayMs: 1, retryMaxDelayMs: 1 };

  it("retries transient 5xx and eventually succeeds", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ error: "oops", message: "boom" }, 500))
      .mockResolvedValueOnce(new Response("Bad Gateway", { status: 502 }))
      .mockResolvedValueOnce(jsonResponse({ id: "u1" }));

    const client = makeClient({ maxRetries: 3, ...RETRY_OPTS });
    const me = await client.getMe();
    expect(me).toEqual({ id: "u1" });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("retries 429 and eventually succeeds", async () => {
    fetchMock
      .mockResolvedValueOnce(
        jsonResponse({ detail: "slow down" }, 429, { "retry-after": "0" }),
      )
      .mockResolvedValueOnce(jsonResponse({ id: "u1" }));

    const client = makeClient({ maxRetries: 2, ...RETRY_OPTS });
    const me = await client.getMe();
    expect(me).toEqual({ id: "u1" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not retry 4xx (other than 429)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ error: "not_found", message: "nope" }, 404),
    );
    const client = makeClient({ maxRetries: 5, ...RETRY_OPTS });
    await expect(client.getMe()).rejects.toThrow(CrowdSorcererError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not retry AuthError (401)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ detail: "Unauthorized" }, 401),
    );
    const client = makeClient({ maxRetries: 5, ...RETRY_OPTS });
    await expect(client.getMe()).rejects.toThrow(AuthError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not retry InsufficientCreditsError (402)", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ required: 5, available: 1 }, 402),
    );
    const client = makeClient({ maxRetries: 5, ...RETRY_OPTS });
    await expect(
      client.submitTask({ type: "pii_detect", input: { text: "x" } }),
    ).rejects.toThrow(InsufficientCreditsError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retries network errors and wraps them in NetworkError", async () => {
    fetchMock
      .mockRejectedValueOnce(new TypeError("fetch failed"))
      .mockResolvedValueOnce(jsonResponse({ id: "u1" }));

    const client = makeClient({ maxRetries: 2, ...RETRY_OPTS });
    const me = await client.getMe();
    expect(me).toEqual({ id: "u1" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("gives up after maxRetries and surfaces the last error", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ error: "boom", message: "still broken" }, 500),
    );
    const client = makeClient({ maxRetries: 2, ...RETRY_OPTS });
    await expect(client.getMe()).rejects.toThrow(CrowdSorcererError);
    // 1 initial attempt + 2 retries = 3 total
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("respects maxRetries: 0 (no retries at all)", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ error: "boom", message: "500" }, 500),
    );
    const client = makeClient({ maxRetries: 0 });
    await expect(client.getMe()).rejects.toThrow(CrowdSorcererError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

// ─── Tasks ───────────────────────────────────────────────────────────────────

describe("tasks", () => {
  it("submitTask sends POST /v1/tasks with correct body", async () => {
    const createResp = { task_id: "task-1", status: "queued", estimated_credits: 1 };
    fetchMock.mockResolvedValueOnce(jsonResponse(createResp));

    const client = makeClient();
    const result = await client.submitTask({
      type: "llm_generate",
      input: { messages: [{ role: "user", content: "Hello" }] },
      priority: "high",
    });

    expect(result.task_id).toBe("task-1");
    expect(result.estimated_credits).toBe(1);

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE_URL}/v1/tasks`);
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body);
    expect(body.type).toBe("llm_generate");
    expect(body.priority).toBe("high");
  });

  it("getTask sends GET /v1/tasks/:id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(TASK_FIXTURE));

    const client = makeClient();
    const task = await client.getTask(TASK_FIXTURE.id);

    expect(task.id).toBe(TASK_FIXTURE.id);
    expect(task.status).toBe("completed");
    expect(fetchMock.mock.calls[0][0]).toBe(
      `${BASE_URL}/v1/tasks/${TASK_FIXTURE.id}`
    );
  });

  it("listTasks builds query string from params", async () => {
    const paginated = {
      items: [TASK_FIXTURE],
      total: 1,
      page: 1,
      page_size: 20,
      has_next: false,
    };
    fetchMock.mockResolvedValueOnce(jsonResponse(paginated));

    const client = makeClient();
    const result = await client.listTasks({
      status: "completed",
      type: "llm_generate",
      page: 2,
      page_size: 10,
    });

    expect(result.items).toHaveLength(1);
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("status=completed");
    expect(url).toContain("type=llm_generate");
    expect(url).toContain("page=2");
    expect(url).toContain("page_size=10");
  });

  it("listTasks works without params", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, page: 1, page_size: 20, has_next: false })
    );
    const client = makeClient();
    const result = await client.listTasks();
    expect(result.items).toHaveLength(0);
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toBe(`${BASE_URL}/v1/tasks`);
  });

  it("cancelTask sends POST /v1/tasks/:id/cancel", async () => {
    fetchMock.mockResolvedValueOnce(noContentResponse());
    const client = makeClient();
    await client.cancelTask("task-1");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE_URL}/v1/tasks/task-1/cancel`);
    expect(init.method).toBe("POST");
  });

  it("runTask polls until completed", async () => {
    const createResp = { task_id: "task-1", status: "queued", estimated_credits: 1 };
    fetchMock
      .mockResolvedValueOnce(jsonResponse(createResp))
      .mockResolvedValueOnce(jsonResponse({ ...TASK_FIXTURE, id: "task-1", status: "running" }))
      .mockResolvedValueOnce(jsonResponse({ ...TASK_FIXTURE, id: "task-1", status: "completed" }));

    const client = makeClient();
    const task = await client.runTask(
      { type: "llm_generate", input: { messages: [{ role: "user", content: "Hi" }] } },
      { pollIntervalMs: 10, timeoutMs: 5000 }
    );

    expect(task.status).toBe("completed");
    expect(fetchMock).toHaveBeenCalledTimes(3); // create + 2 polls
  });

  it("runTask returns failed tasks", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ task_id: "task-1", status: "queued", estimated_credits: 1 }))
      .mockResolvedValueOnce(jsonResponse({ ...TASK_FIXTURE, id: "task-1", status: "failed", error: "Boom" }));

    const client = makeClient();
    const task = await client.runTask(
      { type: "llm_generate", input: { messages: [{ role: "user", content: "Hi" }] } },
      { pollIntervalMs: 10 }
    );
    expect(task.status).toBe("failed");
  });

  it("runTask throws on timeout", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ task_id: "task-1", status: "queued", estimated_credits: 1 }))
      .mockImplementation(() =>
        Promise.resolve(jsonResponse({ ...TASK_FIXTURE, id: "task-1", status: "running" }))
      );

    const client = makeClient();
    await expect(
      client.runTask(
        { type: "llm_generate", input: { messages: [{ role: "user", content: "Hi" }] } },
        { pollIntervalMs: 10, timeoutMs: 50 }
      )
    ).rejects.toThrow("did not complete within timeout");
  });
});

// ─── Typed Task Helpers ──────────────────────────────────────────────────────

describe("typed task helpers", () => {
  const createResp = { task_id: "task-1", status: "queued", estimated_credits: 1 };

  // Each helper calls runTask, which does POST + poll. For these tests we
  // just verify the POST body contains the right type and inputs.
  async function testHelper(
    method: keyof CrowdSorcerer,
    args: unknown[],
    expectedType: string,
    expectedInputKeys: string[]
  ) {
    // Mock create + immediate completion
    fetchMock
      .mockResolvedValueOnce(jsonResponse(createResp))
      .mockResolvedValueOnce(jsonResponse({ ...TASK_FIXTURE, id: "task-1", type: expectedType }));

    const client = makeClient();
    // @ts-expect-error - dynamic method call
    await client[method](...args);

    const [, init] = fetchMock.mock.calls[0];
    const body = JSON.parse(init.body);
    expect(body.type).toBe(expectedType);
    for (const key of expectedInputKeys) {
      expect(body.input).toHaveProperty(key);
    }
  }

  it("webResearch", () =>
    testHelper(
      "webResearch",
      [{ url: "https://example.com", instruction: "Summarise" }],
      "web_research",
      ["url", "instruction"]
    ));

  it("entityLookup", () =>
    testHelper(
      "entityLookup",
      [{ entity_type: "company", name: "Acme" }],
      "entity_lookup",
      ["entity_type", "name"]
    ));

  it("documentParse", () =>
    testHelper(
      "documentParse",
      [{ url: "https://example.com/doc.pdf" }],
      "document_parse",
      ["url"]
    ));

  it("dataTransform", () =>
    testHelper(
      "dataTransform",
      [{ data: [1, 2, 3], transform: "sum" }],
      "data_transform",
      ["data", "transform"]
    ));

  it("llmGenerate", () =>
    testHelper(
      "llmGenerate",
      [{ messages: [{ role: "user", content: "Hello" }] }],
      "llm_generate",
      ["messages"]
    ));

  it("screenshot", () =>
    testHelper(
      "screenshot",
      [{ url: "https://example.com" }],
      "screenshot",
      ["url"]
    ));

  it("audioTranscribe", () =>
    testHelper(
      "audioTranscribe",
      [{ url: "https://example.com/audio.mp3" }],
      "audio_transcribe",
      ["url"]
    ));

  it("piiDetect", () =>
    testHelper(
      "piiDetect",
      [{ text: "John Doe lives at 123 Main St" }],
      "pii_detect",
      ["text"]
    ));

  it("codeExecute", () =>
    testHelper(
      "codeExecute",
      [{ code: "print('hello')", language: "python" }],
      "code_execute",
      ["code", "language"]
    ));

  it("webIntel", () =>
    testHelper(
      "webIntel",
      [{ query: "latest AI news" }],
      "web_intel",
      ["query"]
    ));
});

// ─── Credits ─────────────────────────────────────────────────────────────────

describe("credits", () => {
  it("getCredits sends GET /v1/credits", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ available: 500, reserved: 10, total_used: 100, plan: "free" })
    );
    const client = makeClient();
    const balance = await client.getCredits();
    expect(balance.available).toBe(500);
    expect(balance.plan).toBe("free");
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE_URL}/v1/credits`);
  });

  it("listTransactions builds query params", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, page: 1, page_size: 20, has_next: false })
    );
    const client = makeClient();
    await client.listTransactions({ page: 2, page_size: 10 });

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("page=2");
    expect(url).toContain("page_size=10");
  });

  it("listTransactions works without params", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, page: 1, page_size: 20, has_next: false })
    );
    const client = makeClient();
    await client.listTransactions();
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toBe(`${BASE_URL}/v1/credits/transactions`);
  });
});

// ─── API Keys ────────────────────────────────────────────────────────────────

describe("API keys", () => {
  it("listApiKeys sends GET /v1/api-keys", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse([{ id: "key-1", name: "test", prefix: "cs_", scopes: [], created_at: "2026-01-01T00:00:00Z" }])
    );
    const client = makeClient();
    const keys = await client.listApiKeys();
    expect(keys).toHaveLength(1);
    expect(keys[0].name).toBe("test");
  });

  it("createApiKey sends POST /v1/api-keys", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ id: "key-1", key: "csk_abc", name: "My Key", created_at: "2026-01-01T00:00:00Z" })
    );
    const client = makeClient();
    const result = await client.createApiKey({ name: "My Key", scopes: ["tasks:read"] });
    expect(result.key).toBe("csk_abc");

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.name).toBe("My Key");
    expect(body.scopes).toEqual(["tasks:read"]);
  });

  it("deleteApiKey sends DELETE /v1/api-keys/:id", async () => {
    fetchMock.mockResolvedValueOnce(noContentResponse());
    const client = makeClient();
    await client.deleteApiKey("key-1");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${BASE_URL}/v1/api-keys/key-1`);
    expect(init.method).toBe("DELETE");
  });
});

// ─── User ────────────────────────────────────────────────────────────────────

describe("user", () => {
  it("getMe returns user data", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        id: "user-1",
        email: "test@example.com",
        name: "Test User",
        created_at: "2026-01-01T00:00:00Z",
        plan: "free",
        role: "requester",
        credits: 1000,
      })
    );
    const client = makeClient();
    const user = await client.getMe();
    expect(user.email).toBe("test@example.com");
    expect(user.credits).toBe(1000);
  });

  it("getQuota returns quota status", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        plan: "free",
        tasks: { used: 5, limit: 100, unlimited: false },
        pipeline_runs: { used: 0, limit: 10, unlimited: false },
        pipelines_total: { used: 0, limit: 3, unlimited: false },
        batch_task_size: 50,
        max_worker_assignments: 5,
      })
    );
    const client = makeClient();
    const quota = await client.getQuota();
    expect(quota.plan).toBe("free");
    expect(quota.tasks.used).toBe(5);
  });
});

// ─── Template Marketplace ────────────────────────────────────────────────────

describe("template marketplace", () => {
  const templateFixture = {
    id: "tmpl-1",
    creator_id: "user-1",
    name: "Web Summary",
    description: "Summarise a web page",
    task_type: "web_research",
    execution_mode: "ai",
    category: "research",
    tags: ["web", "summary"],
    task_config: {},
    example_input: { url: "https://example.com" },
    is_public: true,
    is_featured: false,
    use_count: 42,
    rating_sum: 20,
    rating_count: 5,
    avg_rating: 4.0,
    created_at: "2026-01-01T00:00:00Z",
  };

  it("listTemplates with filters", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [templateFixture], total: 1, page: 1, page_size: 24, has_next: false })
    );
    const client = makeClient();
    const result = await client.listTemplates({
      task_type: "web_research",
      category: "research",
      sort: "popular",
      search: "summary",
    });
    expect(result.items).toHaveLength(1);

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("task_type=web_research");
    expect(url).toContain("category=research");
    expect(url).toContain("sort=popular");
    expect(url).toContain("search=summary");
  });

  it("listTemplates with my_own flag", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, page: 1, page_size: 24, has_next: false })
    );
    const client = makeClient();
    await client.listTemplates({ my_own: true });
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("my_own=true");
  });

  it("getTemplate sends GET /v1/marketplace/templates/:id", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(templateFixture));
    const client = makeClient();
    const tmpl = await client.getTemplate("tmpl-1");
    expect(tmpl.name).toBe("Web Summary");
    expect(fetchMock.mock.calls[0][0]).toBe(`${BASE_URL}/v1/marketplace/templates/tmpl-1`);
  });

  it("createTemplate sends POST with body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(templateFixture));
    const client = makeClient();
    await client.createTemplate({
      name: "Web Summary",
      task_type: "web_research",
      description: "Summarise a web page",
      tags: ["web"],
    });

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.name).toBe("Web Summary");
    expect(body.task_type).toBe("web_research");
    expect(body.tags).toEqual(["web"]);
  });

  it("useTemplate sends POST /v1/marketplace/templates/:id/use", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        template_id: "tmpl-1",
        task_type: "web_research",
        execution_mode: "ai",
        task_config: {},
        example_input: { url: "https://example.com" },
      })
    );
    const client = makeClient();
    const result = await client.useTemplate("tmpl-1");
    expect(result.template_id).toBe("tmpl-1");
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
  });

  it("rateTemplate sends POST with rating", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ template_id: "tmpl-1", your_rating: 5, new_avg: 4.5, total_ratings: 6 })
    );
    const client = makeClient();
    const result = await client.rateTemplate("tmpl-1", 5);
    expect(result.your_rating).toBe(5);

    const body = JSON.parse(fetchMock.mock.calls[0][1].body);
    expect(body.rating).toBe(5);
  });

  it("listTemplateCategories sends GET /v1/marketplace/categories", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse([{ category: "research", count: 10 }, { category: "data", count: 5 }])
    );
    const client = makeClient();
    const cats = await client.listTemplateCategories();
    expect(cats).toHaveLength(2);
    expect(cats[0].category).toBe("research");
  });
});

// ─── Worker Marketplace ──────────────────────────────────────────────────────

describe("worker marketplace", () => {
  const marketplaceTask = {
    id: "task-1",
    type: "label_image",
    priority: "normal",
    reward_credits: 3,
    estimated_minutes: 5,
    assignments_required: 3,
    assignments_completed: 1,
    slots_available: 2,
    created_at: "2026-01-01T00:00:00Z",
  };

  it("listMarketplaceTasks with filters", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [marketplaceTask], total: 1, page: 1, page_size: 20, has_next: false })
    );
    const client = makeClient();
    const result = await client.listMarketplaceTasks({
      type: "label_image",
      priority: "high",
      page: 1,
      page_size: 10,
    });
    expect(result.items).toHaveLength(1);

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("type=label_image");
    expect(url).toContain("priority=high");
  });

  it("listMarketplaceTasks works without params", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, page: 1, page_size: 20, has_next: false })
    );
    const client = makeClient();
    await client.listMarketplaceTasks();
    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toBe(`${BASE_URL}/v1/worker/tasks`);
  });

  it("getPersonalisedFeed with pagination", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        items: [{ ...marketplaceTask, match_score: 0.95 }],
        total: 1, page: 1, page_size: 20, has_next: false,
      })
    );
    const client = makeClient();
    const result = await client.getPersonalisedFeed({ page: 1, page_size: 10 });
    expect(result.items[0].match_score).toBe(0.95);

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("page=1");
    expect(url).toContain("page_size=10");
  });
});

// ─── Webhooks ────────────────────────────────────────────────────────────────

describe("webhooks", () => {
  it("listWebhookEvents sends GET /v1/webhooks/events", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        events: [
          { type: "task.completed", description: "Task completed", is_default: true },
          { type: "task.failed", description: "Task failed", is_default: false },
        ],
        default_events: ["task.completed"],
      })
    );
    const client = makeClient();
    const result = await client.listWebhookEvents();
    expect(result.events).toHaveLength(2);
    expect(result.default_events).toContain("task.completed");
  });

  it("getWebhookStats returns delivery stats", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        total_deliveries: 100,
        succeeded: 95,
        failed: 5,
        success_rate: 0.95,
        avg_duration_ms: 250,
        by_event_type: { "task.completed": 80, "task.failed": 20 },
      })
    );
    const client = makeClient();
    const stats = await client.getWebhookStats();
    expect(stats.success_rate).toBe(0.95);
    expect(stats.total_deliveries).toBe(100);
  });

  it("listWebhookLogs with filters", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ items: [], total: 0, page: 1, page_size: 25, has_next: false })
    );
    const client = makeClient();
    await client.listWebhookLogs({
      task_id: "task-1",
      event_type: "task.completed",
      success: true,
      page: 1,
      page_size: 10,
    });

    const url = fetchMock.mock.calls[0][0] as string;
    expect(url).toContain("task_id=task-1");
    expect(url).toContain("event_type=task.completed");
    expect(url).toContain("success=true");
  });
});

// ─── Webhook Verification ────────────────────────────────────────────────────

describe("verifyWebhook", () => {
  const SECRET = "whsec_test_secret_abc123";
  const PAYLOAD = '{"task_id":"task-1","event":"task.completed"}';

  function signPayload(payload: string, secret: string, timestamp?: number): string {
    const crypto = require("crypto");
    const ts = timestamp ?? Math.floor(Date.now() / 1000);
    const sigInput = `${ts}.${payload}`;
    const sig = crypto.createHmac("sha256", secret).update(sigInput).digest("hex");
    return `t=${ts},v1=${sig}`;
  }

  it("returns true for valid signature", () => {
    const sig = signPayload(PAYLOAD, SECRET);
    expect(verifyWebhook(PAYLOAD, SECRET, sig)).toBe(true);
  });

  it("returns false for wrong secret", () => {
    const sig = signPayload(PAYLOAD, SECRET);
    expect(verifyWebhook(PAYLOAD, "wrong_secret", sig)).toBe(false);
  });

  it("returns false for tampered payload", () => {
    const sig = signPayload(PAYLOAD, SECRET);
    expect(verifyWebhook("tampered", SECRET, sig)).toBe(false);
  });

  it("returns false for expired timestamp (default 300s)", () => {
    const oldTs = Math.floor(Date.now() / 1000) - 600; // 10 minutes ago
    const sig = signPayload(PAYLOAD, SECRET, oldTs);
    expect(verifyWebhook(PAYLOAD, SECRET, sig)).toBe(false);
  });

  it("accepts recent timestamp within tolerance", () => {
    const recentTs = Math.floor(Date.now() / 1000) - 60; // 1 minute ago
    const sig = signPayload(PAYLOAD, SECRET, recentTs);
    expect(verifyWebhook(PAYLOAD, SECRET, sig)).toBe(true);
  });

  it("respects custom tolerance", () => {
    const ts = Math.floor(Date.now() / 1000) - 60;
    const sig = signPayload(PAYLOAD, SECRET, ts);
    // 30s tolerance should reject a 60s old signature
    expect(verifyWebhook(PAYLOAD, SECRET, sig, { toleranceSec: 30 })).toBe(false);
    // 120s tolerance should accept it
    expect(verifyWebhook(PAYLOAD, SECRET, sig, { toleranceSec: 120 })).toBe(true);
  });

  it("returns false for missing timestamp in header", () => {
    expect(verifyWebhook(PAYLOAD, SECRET, "v1=abc123")).toBe(false);
  });

  it("returns false for missing v1 signature", () => {
    expect(verifyWebhook(PAYLOAD, SECRET, "t=12345")).toBe(false);
  });

  it("returns false for empty header", () => {
    expect(verifyWebhook(PAYLOAD, SECRET, "")).toBe(false);
  });

  it("returns false for garbage header", () => {
    expect(verifyWebhook(PAYLOAD, SECRET, "completely-invalid")).toBe(false);
  });

  it("works with Uint8Array payload", () => {
    const sig = signPayload(PAYLOAD, SECRET);
    const bytes = new TextEncoder().encode(PAYLOAD);
    expect(verifyWebhook(bytes, SECRET, sig)).toBe(true);
  });
});

// ─── Error Classes ───────────────────────────────────────────────────────────

describe("error classes", () => {
  it("CrowdSorcererError has correct properties", () => {
    const err = new CrowdSorcererError("Something broke", 500, "internal", "req-1");
    expect(err.message).toBe("Something broke");
    expect(err.status).toBe(500);
    expect(err.code).toBe("internal");
    expect(err.requestId).toBe("req-1");
    expect(err.name).toBe("CrowdSorcererError");
    expect(err).toBeInstanceOf(Error);
  });

  it("AuthError defaults", () => {
    const err = new AuthError();
    expect(err.status).toBe(401);
    expect(err.code).toBe("auth_error");
    expect(err.name).toBe("AuthError");
  });

  it("RateLimitError includes retryAfter", () => {
    const err = new RateLimitError(30, "req-2");
    expect(err.retryAfter).toBe(30);
    expect(err.status).toBe(429);
    expect(err.requestId).toBe("req-2");
  });

  it("InsufficientCreditsError includes amounts in message", () => {
    const err = new InsufficientCreditsError(10, 3, "req-3");
    expect(err.message).toContain("10");
    expect(err.message).toContain("3");
    expect(err.status).toBe(402);
  });

  it("error inheritance chain", () => {
    const auth = new AuthError();
    expect(auth).toBeInstanceOf(CrowdSorcererError);
    expect(auth).toBeInstanceOf(Error);

    const rate = new RateLimitError(10);
    expect(rate).toBeInstanceOf(CrowdSorcererError);

    const credits = new InsufficientCreditsError(10, 3);
    expect(credits).toBeInstanceOf(CrowdSorcererError);
  });
});
