/**
 * POST /api/webhooks/endpoints/[endpointId]/rotate-secret
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const POST: APIRoute = async ({ cookies, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response(JSON.stringify({ detail: "Not authenticated" }), { status: 401, headers: { "Content-Type": "application/json" } });

  try {
    const res = await fetch(`${API_URL}/v1/webhooks/endpoints/${params.endpointId}/rotate-secret`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
    });
    const body = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(body), { status: res.status, headers: { "Content-Type": "application/json" } });
  } catch {
    return new Response(JSON.stringify({ detail: "Network error" }), { status: 502, headers: { "Content-Type": "application/json" } });
  }
};
