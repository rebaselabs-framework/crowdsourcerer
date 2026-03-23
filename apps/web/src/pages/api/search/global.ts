/**
 * GET /api/search/global?q=<query>&limit=20 → GET /v1/search/global
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const GET: APIRoute = async ({ cookies, url }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  const q = url.searchParams.get("q") ?? "";
  const limit = url.searchParams.get("limit") ?? "20";
  if (!q) {
    return new Response(
      JSON.stringify({ query: "", total: 0, tasks: [], workers: [], orgs: [] }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  }
  try {
    const qs = new URLSearchParams({ q, limit });
    const res = await fetch(`${API_URL}/v1/search/global?${qs}`, {
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
