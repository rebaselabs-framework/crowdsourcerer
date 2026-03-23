/**
 * POST /api/template-marketplace/[id]/import
 * Imports a public marketplace template into the current user's personal library.
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
    const res = await fetch(`${API_URL}/v1/template-marketplace/${id}/import`, {
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
