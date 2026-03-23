/**
 * POST /api/credits/checkout — proxy to POST /v1/credits/checkout
 * Creates a Stripe checkout session for a credit top-up.
 * Body: { credits: number, success_url: string, cancel_url: string }
 * Returns: { checkout_url: string, session_id: string }
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const POST: APIRoute = async ({ cookies, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  try {
    const body = await request.text();
    const res = await fetch(`${API_URL}/v1/credits/checkout`, {
      method: "POST",
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
