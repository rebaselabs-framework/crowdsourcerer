/**
 * PATCH  /api/webhooks/endpoints/[endpointId]  — update endpoint
 * DELETE /api/webhooks/endpoints/[endpointId]  — delete endpoint
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const PATCH: APIRoute = async ({ cookies, params, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401, headers: { "Content-Type": "application/json" } });

  try {
    const payload = await request.json();
    const res = await fetch(`${API_URL}/v1/webhooks/endpoints/${params.endpointId}`, {
      method: "PATCH",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const body = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(body), { status: res.status, headers: { "Content-Type": "application/json" } });
  } catch {
    return new Response(JSON.stringify({ detail: "Network error" }), { status: 502, headers: { "Content-Type": "application/json" } });
  }
};

export const DELETE: APIRoute = async ({ cookies, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401, headers: { "Content-Type": "application/json" } });

  try {
    const res = await fetch(`${API_URL}/v1/webhooks/endpoints/${params.endpointId}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.status === 204) return new Response(null, { status: 204 });
    const body = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(body), { status: res.status, headers: { "Content-Type": "application/json" } });
  } catch {
    return new Response(JSON.stringify({ detail: "Network error" }), { status: 502, headers: { "Content-Type": "application/json" } });
  }
};
