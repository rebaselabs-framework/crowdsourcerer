/**
 * POST   /api/worker/watchlist/[task_id]  →  POST   /v1/worker/watchlist/{task_id}
 * DELETE /api/worker/watchlist/[task_id]  →  DELETE /v1/worker/watchlist/{task_id}
 * GET    /api/worker/watchlist/[task_id]  →  GET    /v1/worker/watchlist/check/{task_id}
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

async function authHeader(cookies: any) {
  const token = cookies.get("cs_token")?.value;
  if (!token) return null;
  return { Authorization: `Bearer ${token}` };
}

export const POST: APIRoute = async ({ params, cookies }) => {
  const headers = await authHeader(cookies);
  if (!headers) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  try {
    const res = await fetch(`${API_URL}/v1/worker/watchlist/${params.task_id}`, {
      method: "POST",
      headers,
    });
    const data = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ detail: "Network error" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
};

export const DELETE: APIRoute = async ({ params, cookies }) => {
  const headers = await authHeader(cookies);
  if (!headers) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  try {
    const res = await fetch(`${API_URL}/v1/worker/watchlist/${params.task_id}`, {
      method: "DELETE",
      headers,
    });
    const data = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ detail: "Network error" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
};

export const GET: APIRoute = async ({ params, cookies }) => {
  const headers = await authHeader(cookies);
  if (!headers) {
    return new Response(JSON.stringify({ watching: false }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
  try {
    const res = await fetch(
      `${API_URL}/v1/worker/watchlist/check/${params.task_id}`,
      { headers }
    );
    const data = await res.json().catch(() => ({ watching: false }));
    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ watching: false }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }
};
