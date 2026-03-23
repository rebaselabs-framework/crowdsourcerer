/**
 * GET /api/analytics/export — proxy to GET /v1/analytics/export
 * Streams CSV or JSON task analytics export.
 * Query params: fmt=csv|json, days=N, org_id=UUID
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

  // Forward all query params
  const params = url.searchParams.toString();
  const apiUrl = `${API_URL}/v1/analytics/export${params ? `?${params}` : ""}`;

  try {
    const res = await fetch(apiUrl, {
      headers: { Authorization: `Bearer ${token}` },
    });

    if (!res.ok) {
      const body = await res.text();
      return new Response(body, {
        status: res.status,
        headers: { "Content-Type": "application/json" },
      });
    }

    // Forward the content-disposition so the browser triggers a download
    const contentType = res.headers.get("content-type") ?? "text/csv";
    const disposition = res.headers.get("content-disposition") ?? 'attachment; filename="export.csv"';
    const data = await res.arrayBuffer();

    return new Response(data, {
      status: 200,
      headers: {
        "Content-Type": contentType,
        "Content-Disposition": disposition,
      },
    });
  } catch (err: any) {
    return new Response(JSON.stringify({ detail: err.message ?? "Request failed" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
};
