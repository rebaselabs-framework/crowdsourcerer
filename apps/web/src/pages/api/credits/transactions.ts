/**
 * GET /api/credits/transactions?page=N&page_size=N
 * Proxy to GET /v1/credits/transactions — passes httpOnly auth cookie server-side.
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

  const page = url.searchParams.get("page") ?? "1";
  const pageSize = url.searchParams.get("page_size") ?? "20";

  try {
    const res = await fetch(
      `${API_URL}/v1/credits/transactions?page=${page}&page_size=${pageSize}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
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
