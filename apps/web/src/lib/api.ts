/**
 * Server-side API client helpers.
 * Used in Astro SSR pages / API routes.
 */
import type { AstroCookies } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export async function apiFetch<T>(
  path: string,
  init: RequestInit & { token?: string } = {}
): Promise<T> {
  const { token, ...rest } = init;
  const res = await fetch(`${API_URL}${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(rest.headers ?? {}),
    },
  });

  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    // The API returns errors in several formats — normalise to a readable string.
    //  1. {"detail": "string"}                     — standard FastAPI HTTPException
    //  2. {"detail": [{type, loc, msg}]}           — Pydantic validation errors
    //  3. {"detail": {"error": "code", ...}}       — structured credit/rate-limit errors
    //  4. {"error": "code", "message": "..."}      — global exception handler
    //  5. {"error": "Rate limit exceeded: ..."}    — slowapi rate limiter
    let msg: string;
    if (typeof body.detail === "string") {
      msg = body.detail;
    } else if (Array.isArray(body.detail) && body.detail.length > 0) {
      msg = body.detail.map((e: any) => e.msg ?? String(e)).join("; ");
    } else if (typeof body.detail === "object" && body.detail !== null) {
      // Structured error — extract human-readable message from the dict
      msg = body.detail.message ?? body.detail.error ?? JSON.stringify(body.detail);
    } else if (body.message) {
      msg = body.message;
    } else if (body.error) {
      msg = typeof body.error === "string" ? body.error : JSON.stringify(body.error);
    } else {
      msg = `HTTP ${res.status}`;
    }
    throw new Error(msg);
  }

  if (res.status === 204) return undefined as unknown as T;
  return res.json();
}

/**
 * Extract the auth token from an AstroCookies instance.
 * Use: const token = getToken(Astro.cookies)
 * NOTE: Do NOT call Astro.cookies.getAll() — it doesn't exist in Astro 5.
 */
export function getToken(cookies: AstroCookies): string | undefined {
  return cookies.get("cs_token")?.value;
}

/**
 * Read a human-readable error message off an unknown JSON response body.
 * Use in inline `<script>` catch blocks where `apiFetch`'s normalizer
 * isn't available (client code, not SSR).
 */
export function extractDetail(body: unknown, fallback = "Request failed"): string {
  if (typeof body === "object" && body !== null) {
    const d = (body as { detail?: unknown }).detail;
    if (typeof d === "string" && d.length > 0) return d;
  }
  return fallback;
}

/**
 * Race a promise against a timeout.
 * If the promise doesn't resolve within `ms` milliseconds, resolves with `fallback`.
 *
 * Use for non-critical SSR data fetches so slow analytics don't block page render:
 *   overview = await withTimeout(apiFetch("/v1/analytics/overview", { token }), 1500)
 */
export function withTimeout<T>(
  promise: Promise<T>,
  ms: number,
  fallback: T | null = null
): Promise<T | null> {
  const timer = new Promise<null>((resolve) =>
    setTimeout(() => resolve(null), ms)
  );
  return Promise.race([promise.catch(() => fallback), timer]);
}
