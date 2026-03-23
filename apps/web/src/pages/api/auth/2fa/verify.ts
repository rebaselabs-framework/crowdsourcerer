/**
 * POST /api/auth/2fa/verify → POST /v1/auth/2fa/verify
 * Public endpoint — no cookie auth needed (uses pending_token in body).
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const POST: APIRoute = async ({ request, cookies }) => {
  try {
    const body = await request.text();
    const res = await fetch(`${API_URL}/v1/auth/2fa/verify`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    const data = await res.json().catch(() => ({}));

    // If successful, set cookie just like login does
    if (res.ok && data.access_token) {
      cookies.set("cs_token", data.access_token, {
        path: "/",
        httpOnly: true,
        sameSite: "lax",
        maxAge: data.expires_in ?? 86400,
      });
    }

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
