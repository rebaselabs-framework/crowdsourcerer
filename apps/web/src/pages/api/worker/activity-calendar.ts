/**
 * GET /api/worker/activity-calendar?days=14
 * Proxy to GET /v1/worker/activity/calendar — returns active dates for streak calendar.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const GET: APIRoute = async ({ cookies, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response(JSON.stringify({ detail: "Unauthorized" }), { status: 401 });

  const url = new URL(request.url);
  const days = url.searchParams.get("days") ?? "14";

  const res = await fetch(`${API_URL}/v1/worker/activity/calendar?days=${days}`, {
    headers: { Authorization: `Bearer ${token}` },
  });

  const data = await res.json().catch(() => ({}));
  return new Response(JSON.stringify(data), {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
};
