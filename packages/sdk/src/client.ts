import type {
  Task,
  TaskCreateRequest,
  TaskCreateResponse,
  TaskType,
  TaskInput,
  TaskPriority,
  CreditBalance,
  CreditTransaction,
  ApiKey,
  ApiKeyCreateRequest,
  ApiKeyCreateResponse,
  User,
  PaginatedResponse,
  WebResearchInput,
  EntityLookupInput,
  DocumentParseInput,
  DataTransformInput,
  LLMGenerateInput,
  ScreenshotInput,
  AudioTranscribeInput,
  PiiDetectInput,
  CodeExecuteInput,
  WebIntelInput,
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
} from "./errors";

export interface CrowdSorcererOptions {
  apiKey: string;
  baseUrl?: string;
  timeout?: number;
  maxRetries?: number;
}

const DEFAULT_BASE_URL = "https://crowdsourcerer.rebaselabs.online";
const DEFAULT_TIMEOUT = 30_000;
const DEFAULT_MAX_RETRIES = 3;

export class CrowdSorcerer {
  private readonly apiKey: string;
  private readonly baseUrl: string;
  private readonly timeout: number;
  private readonly maxRetries: number;

  constructor(options: CrowdSorcererOptions) {
    if (!options.apiKey) throw new AuthError("apiKey is required");
    this.apiKey = options.apiKey;
    this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/$/, "");
    this.timeout = options.timeout ?? DEFAULT_TIMEOUT;
    this.maxRetries = options.maxRetries ?? DEFAULT_MAX_RETRIES;
  }

  // ─── Internal fetch ─────────────────────────────────────────────────────

  private async fetch<T>(
    path: string,
    init: RequestInit = {}
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeout);

    const response = await globalThis.fetch(url, {
      ...init,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        "Authorization": `Bearer ${this.apiKey}`,
        "X-Client": "crowdsourcerer-sdk/1.0.0",
        ...(init.headers ?? {}),
      },
    }).finally(() => clearTimeout(timer));

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

  /** List tasks with optional filters */
  async listTasks(params?: {
    status?: Task["status"];
    type?: TaskType;
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

  // ─── Typed task helpers ──────────────────────────────────────────────────

  async webResearch(
    input: WebResearchInput,
    opts?: { priority?: TaskPriority; webhook_url?: string }
  ) {
    return this.runTask({ type: "web_research", input, ...opts });
  }

  async entityLookup(
    input: EntityLookupInput,
    opts?: { priority?: TaskPriority; webhook_url?: string }
  ) {
    return this.runTask({ type: "entity_lookup", input, ...opts });
  }

  async documentParse(
    input: DocumentParseInput,
    opts?: { priority?: TaskPriority; webhook_url?: string }
  ) {
    return this.runTask({ type: "document_parse", input, ...opts });
  }

  async dataTransform(
    input: DataTransformInput,
    opts?: { priority?: TaskPriority; webhook_url?: string }
  ) {
    return this.runTask({ type: "data_transform", input, ...opts });
  }

  async llmGenerate(
    input: LLMGenerateInput,
    opts?: { priority?: TaskPriority; webhook_url?: string }
  ) {
    return this.runTask({ type: "llm_generate", input, ...opts });
  }

  async screenshot(
    input: ScreenshotInput,
    opts?: { priority?: TaskPriority; webhook_url?: string }
  ) {
    return this.runTask({ type: "screenshot", input, ...opts });
  }

  async audioTranscribe(
    input: AudioTranscribeInput,
    opts?: { priority?: TaskPriority; webhook_url?: string }
  ) {
    return this.runTask({ type: "audio_transcribe", input, ...opts });
  }

  async piiDetect(
    input: PiiDetectInput,
    opts?: { priority?: TaskPriority; webhook_url?: string }
  ) {
    return this.runTask({ type: "pii_detect", input, ...opts });
  }

  async codeExecute(
    input: CodeExecuteInput,
    opts?: { priority?: TaskPriority; webhook_url?: string }
  ) {
    return this.runTask({ type: "code_execute", input, ...opts });
  }

  async webIntel(
    input: WebIntelInput,
    opts?: { priority?: TaskPriority; webhook_url?: string }
  ) {
    return this.runTask({ type: "web_intel", input, ...opts });
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

// ─── Webhook verification ───────────────────────────────────────────────────

export interface VerifyWebhookOptions {
  /** Maximum age of the delivery in seconds (default: 300 = 5 minutes). */
  toleranceSec?: number;
}

/**
 * Verify a CrowdSorcerer webhook signature.
 *
 * Every webhook delivery includes an `X-Crowdsorcerer-Signature` header
 * in the format `t=TIMESTAMP,v1=HMAC_HEX`. This function verifies that
 * the payload was signed by your endpoint secret and is recent enough
 * to prevent replay attacks.
 *
 * @param payload - The raw request body as a string or Buffer.
 * @param secret - Your webhook endpoint signing secret.
 * @param signatureHeader - Value of the `X-Crowdsorcerer-Signature` header.
 * @param options - Optional tolerance configuration.
 * @returns `true` if the signature is valid and the timestamp is within tolerance.
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
 *   // Handle the event
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
  const tolerance = options?.toleranceSec ?? 300;

  // Parse "t=TIMESTAMP,v1=SIGNATURE[,v0=OLD_SIGNATURE]"
  const parts: Record<string, string> = {};
  for (const segment of signatureHeader.split(",")) {
    const eqIdx = segment.indexOf("=");
    if (eqIdx === -1) continue;
    parts[segment.slice(0, eqIdx).trim()] = segment.slice(eqIdx + 1).trim();
  }

  const timestamp = parts["t"];
  const v1Sig = parts["v1"];
  if (!timestamp || !v1Sig) return false;

  // Replay protection
  if (Math.abs(Date.now() / 1000 - Number(timestamp)) > tolerance) return false;

  // Reconstruct the signed input: "{timestamp}.{payload}"
  const payloadBytes =
    typeof payload === "string" ? new TextEncoder().encode(payload) : payload;
  const tsPrefix = new TextEncoder().encode(`${timestamp}.`);
  const sigInput = new Uint8Array(tsPrefix.length + payloadBytes.length);
  sigInput.set(tsPrefix, 0);
  sigInput.set(payloadBytes, tsPrefix.length);

  // Use Node.js crypto (available in all supported runtimes)
  try {
    const crypto = require("crypto");
    const expected = crypto
      .createHmac("sha256", secret)
      .update(sigInput)
      .digest("hex");
    return crypto.timingSafeEqual(
      Buffer.from(expected, "utf-8"),
      Buffer.from(v1Sig, "utf-8"),
    );
  } catch {
    // Fallback: constant-time-ish comparison (no native crypto)
    const crypto2 = globalThis.crypto;
    if (crypto2?.subtle) {
      // Web Crypto API not available synchronously — return simple compare
      // For production use, prefer the Node.js crypto path above.
    }
    // Simple string comparison (not timing-safe, but functional)
    const encoder = new TextEncoder();
    const key = encoder.encode(secret);
    // Use Web Crypto if available (async fallback not possible in sync fn)
    // For non-Node environments, consider using the async verifyWebhookAsync
    return v1Sig === computeHmacFallback(key, sigInput);
  }
}

/** Minimal fallback HMAC for environments without Node.js crypto. */
function computeHmacFallback(
  _key: Uint8Array,
  _data: Uint8Array,
): string {
  // If we get here, no crypto is available — always fail secure
  return "";
}
