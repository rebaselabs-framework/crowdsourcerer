/**
 * GET  /api/payouts — proxy to GET  /v1/payouts (list)
 * POST /api/payouts — proxy to POST /v1/payouts (create payout request)
 *
 * cs_token is httpOnly so client JS cannot read it directly.
 * This proxy forwards the request server-side with the cookie.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

function authHeaders(token: string, extra: Record<string, string> = {}) {
  return { Authorization: `Bearer ${token}`, ...extra };
}

export const GET: APIRoute = async ({ cookies, url }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  const qs = url.searchParams.toString();
  const res = await fetch(`${API_URL}/v1/payouts${qs ? `?${qs}` : ""}`, {
    headers: authHeaders(token),
  }).catch((err) => new Response(JSON.stringify({ detail: err.message }), { status: 502 }));
  const data = await (res as Response).json().catch(() => ({}));
  return new Response(JSON.stringify(data), {
    status: (res as Response).status,
    headers: { "Content-Type": "application/json" },
  });
};

export const POST: APIRoute = async ({ cookies, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  const body = await request.text();
  try {
    const res = await fetch(`${API_URL}/v1/payouts`, {
      method: "POST",
      headers: authHeaders(token, { "Content-Type": "application/json" }),
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
