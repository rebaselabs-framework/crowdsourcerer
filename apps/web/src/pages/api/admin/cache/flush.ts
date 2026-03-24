/**
 * DELETE /api/admin/cache/flush
 * Proxy to DELETE /v1/admin/cache/flush — flushes task result cache entries.
 *
 * Query params forwarded:
 *   task_type   (optional) — flush only this type
 *   expired_only=true      — only expired entries
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const DELETE: APIRoute = async ({ request, cookies }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Unauthorized" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  // Forward query parameters to the backend
  const url = new URL(request.url);
  const backendUrl = new URL(`${API_URL}/v1/admin/cache/flush`);
  url.searchParams.forEach((v, k) => backendUrl.searchParams.set(k, v));

  try {
    const res = await fetch(backendUrl.toString(), {
      method: "DELETE",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
    });

    const body = await res.text();
    return new Response(body, {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ detail: "Upstream error" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
};
