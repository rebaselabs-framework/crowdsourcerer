/**
 * POST /api/auth/resend-verification → POST /v1/auth/resend-verification
 * Requires authentication. Sends a fresh verification email to the logged-in user.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const POST: APIRoute = async ({ cookies }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  try {
    const res = await fetch(`${API_URL}/v1/auth/resend-verification`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
    });
    const data = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ detail: "Request failed" }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
};
