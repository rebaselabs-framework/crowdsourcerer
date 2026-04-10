export class CrowdSorcererError extends Error {
  public readonly status: number;
  public readonly code: string;
  public readonly requestId?: string;

  constructor(
    message: string,
    status: number,
    code: string,
    requestId?: string
  ) {
    super(message);
    this.name = "CrowdSorcererError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
  }
}

export class AuthError extends CrowdSorcererError {
  constructor(message = "Invalid or missing API key", requestId?: string) {
    super(message, 401, "auth_error", requestId);
    this.name = "AuthError";
  }
}

export class RateLimitError extends CrowdSorcererError {
  public readonly retryAfter?: number;

  constructor(retryAfter?: number, requestId?: string) {
    super("Rate limit exceeded", 429, "rate_limit", requestId);
    this.name = "RateLimitError";
    this.retryAfter = retryAfter;
  }
}

export class TaskError extends CrowdSorcererError {
  public readonly taskId?: string;

  constructor(message: string, taskId?: string, requestId?: string) {
    super(message, 422, "task_error", requestId);
    this.name = "TaskError";
    this.taskId = taskId;
  }
}

export class InsufficientCreditsError extends CrowdSorcererError {
  constructor(required: number, available: number, requestId?: string) {
    super(
      `Insufficient credits: need ${required}, have ${available}`,
      402,
      "insufficient_credits",
      requestId
    );
    this.name = "InsufficientCreditsError";
  }
}

/**
 * Raised when a request fails at the network layer (connection refused,
 * DNS failure, per-request timeout, etc.) — before any HTTP response is
 * received. These errors are always retryable.
 *
 * The original cause is preserved on `.cause` for debugging.
 */
export class NetworkError extends CrowdSorcererError {
  constructor(cause: unknown, requestId?: string) {
    const causeMsg =
      cause instanceof Error ? cause.message : String(cause ?? "network error");
    super(`Network error: ${causeMsg}`, 0, "network_error", requestId);
    this.name = "NetworkError";
    // Preserve the underlying error for consumers that want to inspect it.
    (this as { cause?: unknown }).cause = cause;
  }
}
