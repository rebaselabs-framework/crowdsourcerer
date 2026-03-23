/**
 * GET  /api/notifications/digest-prefs  → GET  /v1/notifications/digest-prefs
 * PUT  /api/notifications/digest-prefs  → PUT  /v1/notifications/digest-prefs
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const GET: APIRoute = async ({ cookies }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  try {
    const res = await fetch(`${API_URL}/v1/notifications/digest-prefs`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const body = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(body), {
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

export const PUT: APIRoute = async ({ cookies, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  try {
    const body = await request.json();
    const res = await fetch(`${API_URL}/v1/notifications/digest-prefs`, {
      method: "PUT",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    const resBody = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(resBody), {
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
