/**
 * PATCH  /api/worker/saved-searches/[id] → PATCH  /v1/worker/saved-searches/{id}
 * DELETE /api/worker/saved-searches/[id] → DELETE /v1/worker/saved-searches/{id}
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

async function proxy(method: string, token: string, id: string, body?: string): Promise<Response> {
  const res = await fetch(`${API_URL}/v1/worker/saved-searches/${id}`, {
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

export const PATCH: APIRoute = async ({ cookies, request, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401, headers: { "Content-Type": "application/json" } });
  try { return await proxy("PATCH", token, params.id!, await request.text()); }
  catch { return new Response(JSON.stringify({ detail: "Network error" }), { status: 502, headers: { "Content-Type": "application/json" } }); }
};

export const DELETE: APIRoute = async ({ cookies, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401, headers: { "Content-Type": "application/json" } });
  try { return await proxy("DELETE", token, params.id!); }
  catch { return new Response(JSON.stringify({ detail: "Network error" }), { status: 502, headers: { "Content-Type": "application/json" } }); }
};
