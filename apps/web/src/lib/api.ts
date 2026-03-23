/**
 * Server-side API client helpers.
 * Used in Astro SSR pages / API routes.
 */
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
    throw new Error(body.detail ?? body.message ?? `HTTP ${res.status}`);
  }

  if (res.status === 204) return undefined as unknown as T;
  return res.json();
}

export function getToken(cookies: Record<string, string>): string | undefined {
  return cookies["cs_token"];
}
