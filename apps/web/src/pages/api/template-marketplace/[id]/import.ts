/**
 * POST /api/template-marketplace/[id]/import
 * Marks a public marketplace template as used and returns its config for pre-filling.
 * Proxies to POST /v1/marketplace/templates/{id}/use (increments use_count).
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const POST: APIRoute = async ({ cookies, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  const { id } = params;

  try {
    const res = await fetch(`${API_URL}/v1/marketplace/templates/${id}/use`, {
      method: "POST",
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
