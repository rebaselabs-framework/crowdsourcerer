/**
 * Widget API — public task feed endpoint with open CORS.
 * Allows any website to embed the CrowdSorcerer task feed widget.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

const CORS_HEADERS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Content-Type": "application/json",
  "Cache-Control": "public, max-age=30, s-maxage=30",
};

export const GET: APIRoute = async ({ url }) => {
  const type = url.searchParams.get("type") ?? "";
  const limit = Math.min(parseInt(url.searchParams.get("limit") ?? "6", 10), 20);

  try {
    const params = new URLSearchParams({ page: "1", page_size: String(limit) });
    if (type) params.set("type", type);

    const res = await fetch(`${API_URL}/v1/tasks/public?${params}`);
    if (!res.ok) {
      return new Response(JSON.stringify({ error: "Failed to fetch tasks" }), {
        status: 502,
        headers: CORS_HEADERS,
      });
    }

    const data = await res.json();
    return new Response(JSON.stringify(data), {
      status: 200,
      headers: CORS_HEADERS,
    });
  } catch {
    return new Response(JSON.stringify({ error: "Service unavailable" }), {
      status: 503,
      headers: CORS_HEADERS,
    });
  }
};

// Handle preflight
export const OPTIONS: APIRoute = async () => {
  return new Response(null, { status: 204, headers: CORS_HEADERS });
};
