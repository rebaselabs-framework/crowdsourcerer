/**
 * GET  /api/worker/saved-searches       → GET  /v1/worker/saved-searches
 * POST /api/worker/saved-searches       → POST /v1/worker/saved-searches
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

async function proxy(method: string, token: string, body?: string): Promise<Response> {
  const res = await fetch(`${API_URL}/v1/worker/saved-searches`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body,
  });
  if (res.status === 204) return new Response(null, { status: 204 });
  const data = await res.json().catch(() => ({}));
  return new Response(JSON.stringify(data), {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}

export const GET: APIRoute = async ({ cookies }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401, headers: { "Content-Type": "application/json" } });
  try { return await proxy("GET", token); }
  catch { return new Response(JSON.stringify({ detail: "Network error" }), { status: 502, headers: { "Content-Type": "application/json" } }); }
};

export const POST: APIRoute = async ({ cookies, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401, headers: { "Content-Type": "application/json" } });
  try { return await proxy("POST", token, await request.text()); }
  catch { return new Response(JSON.stringify({ detail: "Network error" }), { status: 502, headers: { "Content-Type": "application/json" } }); }
};
