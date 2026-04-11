import type {
  Task,
  TaskCreateRequest,
  TaskCreateResponse,
  PipelineStepTaskType,
  CreditBalance,
  CreditTransaction,
  ApiKey,
  ApiKeyCreateRequest,
  ApiKeyCreateResponse,
  User,
  PaginatedResponse,
  Template,
  TemplateCreateRequest,
  TemplateUseResponse,
  TemplateRateResponse,
  QuotaStatus,
  MarketplaceTask,
  WebhookLog,
  WebhookStats,
  WebhookEventInfo,
  WebhookEventType,
} from "@crowdsourcerer/types";
import {
  CrowdSorcererError,
  AuthError,
  RateLimitError,
  InsufficientCreditsError,
  NetworkError,
} from "./errors";

export interface CrowdSorcererOptions {
  apiKey: string;
  baseUrl?: string;
  /** Per-request timeout in ms. Each retry attempt gets its own timeout. */
  timeout?: number;
  /**
   * Maximum number of retries after the initial attempt. Set to 0 to disable.
   * Retries apply to transient failures only: network errors, 429, and 5xx.
   */
  maxRetries?: number;
  /** Base delay (ms) for exponential backoff. Defaults to 250. */
  retryBaseDelayMs?: number;
  /** Hard cap (ms) on a single backoff interval. Defaults to 8000. */
  retryMaxDelayMs?: number;
}

const DEFAULT_BASE_URL = "https://crowdsourcerer.rebaselabs.online";
const DEFAULT_TIMEOUT = 30_000;
const DEFAULT_MAX_RETRIES = 3;
const DEFAULT_RETRY_BASE_MS = 250;
const DEFAULT_RETRY_MAX_MS = 8_000;

export class CrowdSorcerer {
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly timeout: number;
  private readonly maxRetries: number;
  private readonly retryBaseDelayMs: number;
  private readonly retryMaxDelayMs: number;

  constructor(options: CrowdSorcererOptions) {
    if (!options.apiKey) throw new AuthError("apiKey is required");
    this.apiKey = options.apiKey;
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    this.timeout = options.timeout ?? DEFAULT_TIMEOUT;
    this.maxRetries = options.maxRetries ?? DEFAULT_MAX_RETRIES;
    this.retryBaseDelayMs = options.retryBaseDelayMs ?? DEFAULT_RETRY_BASE_MS;
    this.retryMaxDelayMs = options.retryMaxDelayMs ?? DEFAULT_RETRY_MAX_MS;
  }

  // ─── Internal fetch + retry ──────────────────────────────────────────────

  /**
   * Execute a request with automatic retries on transient failures.
   *
   * Retryable: network errors, per-request timeouts, HTTP 429, and HTTP 5xx.
   * Not retryable: 4xx other than 429 (AuthError, InsufficientCreditsError,
   * generic client errors).
   *
   * Backoff is exponential with full jitter, capped at `retryMaxDelayMs`.
   * On 429 the `Retry-After` header (if present) takes precedence.
   */
  private async fetch<T>(path: string, init: RequestInit = {}): Promise<T> {
    let attempt = 0;
    while (true) {
      try {
        return await this.fetchOnce<T>(path, init);
      } catch (err) {
        if (attempt >= this.maxRetries || !isRetryableError(err)) throw err;
        const delay = computeBackoffMs(
          err,
          attempt,
          this.retryBaseDelayMs,
          this.retryMaxDelayMs,
        );
        attempt += 1;
        await sleep(delay);
      }
    }
  }

  private async fetchOnce<T>(
    path: string,
    init: RequestInit,
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);

    let response: Response;
    try {
      response = await globalThis.fetch(url, {
        ...init,
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          "Authorization": `Bearer ${this.apiKey}`,
          "X-Client": "crowdsourcerer-sdk/1.0.0",
          ...(init.headers ?? {}),
        },
      });
    } catch (err) {
      // Network error or per-request timeout (AbortError). Wrap so the
      // retry layer can recognise it as transient.
      throw new NetworkError(err);
    } finally {
      clearTimeout(timer);
    }

    const requestId = response.headers.get("x-request-id") ?? undefined;

    if (response.status === 401) throw new AuthError(undefined, requestId);
    if (response.status === 429) {
      const retryAfter = Number(response.headers.get("retry-after") ?? 60);
      throw new RateLimitError(retryAfter, requestId);
    }
    if (response.status === 402) {
      const body = await response.json().catch(() => ({}));
      throw new InsufficientCreditsError(
        body.required ?? 0,
        body.available ?? 0,
        requestId
      );
    }

    if (!response.ok) {
      const body = await response.json().catch(() => ({
        error: "unknown_error",
        message: `HTTP ${response.status}`,
      }));
      throw new CrowdSorcererError(
        body.message ?? "Request failed",
        response.status,
        body.error ?? "unknown_error",
        requestId
      );
    }

    if (response.status === 204) return undefined as unknown as T;
    return response.json() as Promise<T>;
  }

  // ─── Tasks ───────────────────────────────────────────────────────────────

  /** Submit a task and return immediately with the task ID */
  async submitTask(req: TaskCreateRequest): Promise<TaskCreateResponse> {
    return this.fetch<TaskCreateResponse>("/v1/tasks", {
      method: "POST",
      body: JSON.stringify(req),
    });
  }

  /** Get a task by ID */
  async getTask(taskId: string): Promise<Task> {
    return this.fetch<Task>(`/v1/tasks/${taskId}`);
  }

  /** List tasks with optional filters. Type filter accepts any stored
   *  task type, including pipeline-emitted AI steps. */
  async listTasks(params?: {
    status?: Task["status"];
    type?: PipelineStepTaskType;
    page?: number;
    page_size?: number;
  }): Promise<PaginatedResponse<Task>> {
    const qs = new URLSearchParams();
    if (params?.status) qs.set("status", params.status);
    if (params?.type) qs.set("type", params.type);
    if (params?.page) qs.set("page", String(params.page));
    if (params?.page_size) qs.set("page_size", String(params.page_size));
    return this.fetch<PaginatedResponse<Task>>(
      `/v1/tasks${qs.size ? `?${qs}` : ""}`
    );
  }

  /** Cancel a pending/queued task */
  async cancelTask(taskId: string): Promise<void> {
    return this.fetch<void>(`/v1/tasks/${taskId}/cancel`, { method: "POST" });
  }

  /**
   * Submit a task and wait for completion (polls).
   * Max wait: 5 minutes.
   */
  async runTask(
    req: TaskCreateRequest,
    opts: { pollIntervalMs?: number; timeoutMs?: number } = {}
  ): Promise<Task> {
    const { task_id } = await this.submitTask(req);
    const interval = opts.pollIntervalMs ?? 1_500;
    const deadline = Date.now() + (opts.timeoutMs ?? 5 * 60_000);

    while (Date.now() < deadline) {
      await sleep(interval);
      const task = await this.getTask(task_id);
      if (task.status === "completed" || task.status === "failed") {
        return task;
      }
    }

    throw new CrowdSorcererError(
      `Task ${task_id} did not complete within timeout`,
      408,
      "timeout"
    );
  }

  // ─── Credits ─────────────────────────────────────────────────────────────

  async getCredits(): Promise<CreditBalance> {
    return this.fetch<CreditBalance>("/v1/credits");
  }

  async listTransactions(params?: {
    page?: number;
    page_size?: number;
  }): Promise<PaginatedResponse<CreditTransaction>> {
    const qs = new URLSearchParams();
    if (params?.page) qs.set("page", String(params.page));
    if (params?.page_size) qs.set("page_size", String(params.page_size));
    return this.fetch<PaginatedResponse<CreditTransaction>>(
      `/v1/credits/transactions${qs.size ? `?${qs}` : ""}`
    );
  }

  // ─── API Keys ────────────────────────────────────────────────────────────

  async listApiKeys(): Promise<ApiKey[]> {
    return this.fetch<ApiKey[]>("/v1/api-keys");
  }

  async createApiKey(
    req: ApiKeyCreateRequest
  ): Promise<ApiKeyCreateResponse> {
    return this.fetch<ApiKeyCreateResponse>("/v1/api-keys", {
      method: "POST",
      body: JSON.stringify(req),
    });
  }

  async deleteApiKey(keyId: string): Promise<void> {
    return this.fetch<void>(`/v1/api-keys/${keyId}`, { method: "DELETE" });
  }

  // ─── User ────────────────────────────────────────────────────────────────

  async getMe(): Promise<User> {
    return this.fetch<User>("/v1/users/me");
  }

  // ─── Quota ───────────────────────────────────────────────────────────────

  async getQuota(): Promise<QuotaStatus> {
    return this.fetch<QuotaStatus>("/v1/users/quota");
  }

  // ─── Template Marketplace ─────────────────────────────────────────────────

  async listTemplates(params?: {
    page?: number;
    page_size?: number;
    task_type?: string;
    category?: string;
    execution_mode?: string;
    search?: string;
    sort?: "featured" | "popular" | "newest" | "top_rated";
    my_own?: boolean;
  }): Promise<PaginatedResponse<Template>> {
    const qs = new URLSearchParams();
    if (params?.page) qs.set("page", String(params.page));
    if (params?.page_size) qs.set("page_size", String(params.page_size));
    if (params?.task_type) qs.set("task_type", params.task_type);
    if (params?.category) qs.set("category", params.category);
    if (params?.execution_mode) qs.set("execution_mode", params.execution_mode);
    if (params?.search) qs.set("search", params.search);
    if (params?.sort) qs.set("sort", params.sort);
    if (params?.my_own) qs.set("my_own", "true");
    return this.fetch<PaginatedResponse<Template>>(
      `/v1/marketplace/templates${qs.size ? `?${qs}` : ""}`
    );
  }

  async getTemplate(templateId: string): Promise<Template> {
    return this.fetch<Template>(`/v1/marketplace/templates/${templateId}`);
  }

  async createTemplate(req: TemplateCreateRequest): Promise<Template> {
    return this.fetch<Template>("/v1/marketplace/templates", {
      method: "POST",
      body: JSON.stringify(req),
    });
  }

  async useTemplate(templateId: string): Promise<TemplateUseResponse> {
    return this.fetch<TemplateUseResponse>(
      `/v1/marketplace/templates/${templateId}/use`,
      { method: "POST" }
    );
  }

  async rateTemplate(
    templateId: string,
    rating: number
  ): Promise<TemplateRateResponse> {
    return this.fetch<TemplateRateResponse>(
      `/v1/marketplace/templates/${templateId}/rate`,
      { method: "POST", body: JSON.stringify({ rating }) }
    );
  }

  async listTemplateCategories(): Promise<Array<{ category: string; count: number }>> {
    return this.fetch<Array<{ category: string; count: number }>>(
      "/v1/marketplace/categories"
    );
  }

  // ─── Worker marketplace ──────────────────────────────────────────────────

  /**
   * Browse open human tasks (chronological order, supports filters).
   */
  async listMarketplaceTasks(params?: {
    type?: string;
    priority?: string;
    page?: number;
    page_size?: number;
  }): Promise<PaginatedResponse<MarketplaceTask>> {
    const qs = new URLSearchParams();
    if (params?.type) qs.set("type", params.type);
    if (params?.priority) qs.set("priority", params.priority);
    if (params?.page) qs.set("page", String(params.page));
    if (params?.page_size) qs.set("page_size", String(params.page_size));
    return this.fetch<PaginatedResponse<MarketplaceTask>>(
      `/v1/worker/tasks${qs.size ? `?${qs}` : ""}`
    );
  }

  /**
   * Get a skill-ranked feed of tasks personalised for the authenticated worker.
   * Each task includes a `match_score` (0.0–1.0) field.
   */
  async getPersonalisedFeed(params?: {
    page?: number;
    page_size?: number;
  }): Promise<PaginatedResponse<MarketplaceTask>> {
    const qs = new URLSearchParams();
    if (params?.page) qs.set("page", String(params.page));
    if (params?.page_size) qs.set("page_size", String(params.page_size));
    return this.fetch<PaginatedResponse<MarketplaceTask>>(
      `/v1/worker/tasks/feed${qs.size ? `?${qs}` : ""}`
    );
  }

  // ─── Webhooks ────────────────────────────────────────────────────────────

  /**
   * List all supported webhook event types.
   */
  async listWebhookEvents(): Promise<{ events: WebhookEventInfo[]; default_events: WebhookEventType[] }> {
    return this.fetch("/v1/webhooks/events");
  }

  /**
   * Get webhook delivery stats for the authenticated user.
   */
  async getWebhookStats(): Promise<WebhookStats> {
    return this.fetch<WebhookStats>("/v1/webhooks/stats");
  }

  /**
   * List webhook delivery logs.
   */
  async listWebhookLogs(params?: {
    task_id?: string;
    event_type?: WebhookEventType;
    success?: boolean;
    page?: number;
    page_size?: number;
  }): Promise<PaginatedResponse<WebhookLog>> {
    const qs = new URLSearchParams();
    if (params?.task_id) qs.set("task_id", params.task_id);
    if (params?.event_type) qs.set("event_type", params.event_type);
    if (params?.success != null) qs.set("success", String(params.success));
    if (params?.page) qs.set("page", String(params.page));
    if (params?.page_size) qs.set("page_size", String(params.page_size));
    return this.fetch<PaginatedResponse<WebhookLog>>(
      `/v1/webhooks/logs${qs.size ? `?${qs}` : ""}`
    );
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ─── Retry helpers ──────────────────────────────────────────────────────────

/**
 * Classify an error as retryable or permanent.
 *
 * Retryable: network/timeout errors, 429 rate limits, and any 5xx.
 * Permanent: auth failures, credit errors, 4xx client errors other than 429.
 */
function isRetryableError(err: unknown): boolean {
  if (err instanceof NetworkError) return true;
  if (err instanceof RateLimitError) return true;
  if (err instanceof AuthError) return false;
  if (err instanceof InsufficientCreditsError) return false;
  if (err instanceof CrowdSorcererError) return err.status >= 500;
  return false;
}

/**
 * Compute backoff for the next retry attempt. Uses exponential backoff
 * with full jitter (AWS architecture blog — "Exponential Backoff And
 * Jitter"). For 429 responses the server-supplied Retry-After header
 * takes precedence, capped at `maxDelayMs`.
 *
 * @param err       Last thrown error from the previous attempt.
 * @param attempt   0-indexed attempt number that just failed.
 * @param baseMs    Base delay (first retry waits up to baseMs).
 * @param maxDelayMs Hard cap on any individual sleep.
 */
function computeBackoffMs(
  err: unknown,
  attempt: number,
  baseMs: number,
  maxDelayMs: number,
): number {
  if (err instanceof RateLimitError && typeof err.retryAfter === "number" && err.retryAfter > 0) {
    return Math.min(err.retryAfter * 1000, maxDelayMs);
  }
  // Exponential window: base, 2×base, 4×base, ... capped.
  const window = Math.min(baseMs * 2 ** attempt, maxDelayMs);
  // Full jitter — pick uniformly in [0, window).
  return Math.floor(Math.random() * window);
}

// ─── Webhook verification ───────────────────────────────────────────────────

export interface VerifyWebhookOptions {
  /** Maximum age of the delivery in seconds (default: 300 = 5 minutes). */
  toleranceSec?: number;
}

/**
 * Parsed webhook signature header + reconstructed signing input.
 *
 * Shared between `verifyWebhook` (Node crypto) and `verifyWebhookAsync`
 * (Web Crypto) so the parsing, replay-check, and buffer assembly logic
 * only lives in one place.
 *
 * `sigInputBuffer` is returned as a concrete `ArrayBuffer` (not a
 * `Uint8Array` view) so it flows into `SubtleCrypto.sign` without any
 * `ArrayBufferLike` / `SharedArrayBuffer` type gymnastics.
 */
interface ParsedSignature {
  v1Sig: string;
  sigInputBuffer: ArrayBuffer;
}

function parseSignatureHeader(
  payload: string | Uint8Array,
  signatureHeader: string,
  toleranceSec: number,
): ParsedSignature | null {
  // Parse "t=TIMESTAMP,v1=SIGNATURE[,v0=OLD_SIGNATURE]"
  const parts: Record<string, string> = {};
  for (const segment of signatureHeader.split(",")) {
    const eqIdx = segment.indexOf("=");
    if (eqIdx === -1) continue;
    parts[segment.slice(0, eqIdx).trim()] = segment.slice(eqIdx + 1).trim();
  }

  const timestamp = parts["t"];
  const v1Sig = parts["v1"];
  if (!timestamp || !v1Sig) return null;

  // Replay protection
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > toleranceSec) return null;

  // Reconstruct the signed input: "{timestamp}.{payload}" into a fresh,
  // owned ArrayBuffer so crypto APIs have an unambiguous buffer type.
  const payloadBytes =
    typeof payload === "string" ? new TextEncoder().encode(payload) : payload;
  const tsPrefix = new TextEncoder().encode(`${timestamp}.`);
  const sigInputBuffer = new ArrayBuffer(tsPrefix.length + payloadBytes.length);
  const view = new Uint8Array(sigInputBuffer);
  view.set(tsPrefix, 0);
  view.set(payloadBytes, tsPrefix.length);

  return { v1Sig, sigInputBuffer };
}

/** Constant-time comparison of two equal-length hex strings. */
function constantTimeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

/**
 * Verify a CrowdSorcerer webhook signature (Node.js / Bun).
 *
 * Every webhook delivery includes an `X-Crowdsorcerer-Signature` header
 * in the format `t=TIMESTAMP,v1=HMAC_HEX`. This function verifies that
 * the payload was signed by your endpoint secret and is recent enough
 * to prevent replay attacks.
 *
 * Requires Node.js `crypto` or Bun. For Edge / Deno / browser runtimes
 * that only expose Web Crypto, use {@link verifyWebhookAsync} instead.
 *
 * @param payload - The raw request body as a string or Buffer.
 * @param secret - Your webhook endpoint signing secret.
 * @param signatureHeader - Value of the `X-Crowdsorcerer-Signature` header.
 * @param options - Optional tolerance configuration.
 * @returns `true` if the signature is valid and the timestamp is within tolerance.
 *
 * @throws {CrowdSorcererError} if Node `crypto` is not available — call
 *   {@link verifyWebhookAsync} from non-Node runtimes.
 *
 * @example
 * ```ts
 * import { verifyWebhook } from "@crowdsourcerer/sdk";
 *
 * app.post("/webhook", (req, res) => {
 *   const sig = req.headers["x-crowdsorcerer-signature"] as string;
 *   if (!verifyWebhook(req.body, process.env.WEBHOOK_SECRET!, sig)) {
 *     return res.status(401).send("Invalid signature");
 *   }
 *   res.sendStatus(200);
 * });
 * ```
 */
export function verifyWebhook(
  payload: string | Uint8Array,
  secret: string,
  signatureHeader: string,
  options?: VerifyWebhookOptions,
): boolean {
  const parsed = parseSignatureHeader(
    payload,
    signatureHeader,
    options?.toleranceSec ?? 300,
  );
  if (!parsed) return false;

  // Node.js / Bun path. We lazily require to keep this file bundleable in
  // browser builds — the require is only hit when this function is called.
  let nodeCrypto: typeof import("crypto");
  try {
    // eslint-disable-next-line @typescript-eslint/no-var-requires
    nodeCrypto = require("crypto");
  } catch (err) {
    throw new CrowdSorcererError(
      "verifyWebhook requires Node.js `crypto`. Use verifyWebhookAsync in " +
        "Edge / Deno / browser runtimes.",
      0,
      "crypto_unavailable",
    );
  }

  const expected = nodeCrypto
    .createHmac("sha256", secret)
    .update(Buffer.from(parsed.sigInputBuffer))
    .digest("hex");

  // timingSafeEqual requires equal-length buffers; fall back to our own
  // constant-time comparator if the lengths differ (invalid signature).
  if (expected.length !== parsed.v1Sig.length) return false;
  return nodeCrypto.timingSafeEqual(
    Buffer.from(expected, "utf-8"),
    Buffer.from(parsed.v1Sig, "utf-8"),
  );
}

/**
 * Async webhook verification using the Web Crypto API.
 *
 * Works in any runtime that exposes `globalThis.crypto.subtle`: modern
 * browsers, Deno, Cloudflare Workers, Vercel Edge, and recent Node.js.
 *
 * @throws {CrowdSorcererError} if Web Crypto is not available.
 */
export async function verifyWebhookAsync(
  payload: string | Uint8Array,
  secret: string,
  signatureHeader: string,
  options?: VerifyWebhookOptions,
): Promise<boolean> {
  const parsed = parseSignatureHeader(
    payload,
    signatureHeader,
    options?.toleranceSec ?? 300,
  );
  if (!parsed) return false;

  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    throw new CrowdSorcererError(
      "verifyWebhookAsync requires Web Crypto (globalThis.crypto.subtle).",
      0,
      "crypto_unavailable",
    );
  }

  // Copy the UTF-8 encoded secret into an owned ArrayBuffer for the
  // same reason as sigInputBuffer above.
  const secretBytes = new TextEncoder().encode(secret);
  const keyBuffer = new ArrayBuffer(secretBytes.length);
  new Uint8Array(keyBuffer).set(secretBytes);
  const key = await subtle.importKey(
    "raw",
    keyBuffer,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await subtle.sign("HMAC", key, parsed.sigInputBuffer);

  // Convert the raw HMAC bytes to lowercase hex so we can compare against
  // the wire-format `v1=<hex>` field.
  const bytes = new Uint8Array(signature);
  let expectedHex = "";
  for (let i = 0; i < bytes.length; i++) {
    expectedHex += bytes[i].toString(16).padStart(2, "0");
  }

  return constantTimeEqualHex(expectedHex, parsed.v1Sig.toLowerCase());
}
