/**
 * GET    /api/task-templates/[id]       → get one template
 * PATCH  /api/task-templates/[id]       → update template
 * DELETE /api/task-templates/[id]       → delete template
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

function auth(cookies: Parameters<APIRoute>[0]["cookies"]) {
  return cookies.get("cs_token")?.value ?? null;
}

export const GET: APIRoute = async ({ cookies, params }) => {
  const token = auth(cookies);
  if (!token) return new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401, headers: { "Content-Type": "application/json" } });
  try {
    const res = await fetch(`${API_URL}/v1/task-templates/${params.id}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const data = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(data), { status: res.status, headers: { "Content-Type": "application/json" } });
  } catch {
    return new Response(JSON.stringify({ detail: "Network error" }), { status: 502, headers: { "Content-Type": "application/json" } });
  }
};

export const PATCH: APIRoute = async ({ cookies, params, request }) => {
  const token = auth(cookies);
  if (!token) return new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401, headers: { "Content-Type": "application/json" } });
  let body: unknown;
  try { body = await request.json(); } catch { return new Response(JSON.stringify({ detail: "Invalid JSON" }), { status: 400, headers: { "Content-Type": "application/json" } }); }
  try {
    const res = await fetch(`${API_URL}/v1/task-templates/${params.id}`, {
      method: "PATCH",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(data), { status: res.status, headers: { "Content-Type": "application/json" } });
  } catch {
    return new Response(JSON.stringify({ detail: "Network error" }), { status: 502, headers: { "Content-Type": "application/json" } });
  }
};

export const DELETE: APIRoute = async ({ cookies, params }) => {
  const token = auth(cookies);
  if (!token) return new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401, headers: { "Content-Type": "application/json" } });
  try {
    const res = await fetch(`${API_URL}/v1/task-templates/${params.id}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.status === 204) return new Response(null, { status: 204 });
    const data = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(data), { status: res.status, headers: { "Content-Type": "application/json" } });
  } catch {
    return new Response(JSON.stringify({ detail: "Network error" }), { status: 502, headers: { "Content-Type": "application/json" } });
  }
};
