/**
 * GET /api/tasks/[id]/related
 * Proxies to GET /v1/tasks/{id}/related
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const GET: APIRoute = async ({ cookies, params, url }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  const { id } = params;
  const limit = url.searchParams.get("limit") ?? "6";

  try {
    const res = await fetch(`${API_URL}/v1/tasks/${id}/related?limit=${limit}`, {
      headers: { Authorization: `Bearer ${token}` },
    });

    const body = await res.json().catch(() => []);
    return new Response(JSON.stringify(body), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify([]), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
};
