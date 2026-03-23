/**
 * POST /api/admin/reputation/recalculate
 * Proxy to backend to recalculate all worker reputation scores.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const POST: APIRoute = async ({ cookies, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response(JSON.stringify({ detail: "Unauthorized" }), { status: 401 });

  const res = await fetch(`${API_URL}/v1/admin/reputation/recalculate`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
  });

  const body = await res.json().catch(() => ({}));
  return new Response(JSON.stringify(body), {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
};
