/**
 * GET  /api/template-marketplace — list public marketplace templates
 * POST /api/template-marketplace — (not used; import is at /{id}/import)
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

  const params = url.searchParams.toString();
  const apiUrl = `${API_URL}/v1/template-marketplace${params ? `?${params}` : ""}`;

  try {
    const res = await fetch(apiUrl, {
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
    });
    const data = await res.json();
    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch (err: any) {
    return new Response(
      JSON.stringify({ detail: err.message ?? "Request failed" }),
      { status: 502, headers: { "Content-Type": "application/json" } }
    );
  }
};
