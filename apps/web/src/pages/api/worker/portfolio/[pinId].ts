/**
 * PATCH /api/worker/portfolio/[pinId]  — update caption/order of a portfolio pin
 * DELETE /api/worker/portfolio/[pinId] — remove a pin from the portfolio
 * Proxies to PATCH/DELETE /v1/worker/portfolio/{pin_id}
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const PATCH: APIRoute = async ({ cookies, params, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  const body = await request.text();
  try {
    const res = await fetch(`${API_URL}/v1/worker/portfolio/${params.pinId}`, {
      method: "PATCH",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body,
    });
    const data = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err: any) {
    return new Response(JSON.stringify({ detail: err.message ?? "Request failed" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
};

export const DELETE: APIRoute = async ({ cookies, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  try {
    const res = await fetch(`${API_URL}/v1/worker/portfolio/${params.pinId}`, {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    });
    if (res.status === 204) return new Response(null, { status: 204 });
    const data = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err: any) {
    return new Response(JSON.stringify({ detail: err.message ?? "Request failed" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
};
