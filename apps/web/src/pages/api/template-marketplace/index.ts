/**
 * GET  /api/template-marketplace — list marketplace templates
 * POST /api/template-marketplace — create a new marketplace template
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
  const apiUrl = `${API_URL}/v1/marketplace/templates${params ? `?${params}` : ""}`;

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

export const POST: APIRoute = async ({ cookies, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  const body = await request.text();
  try {
    const res = await fetch(`${API_URL}/v1/marketplace/templates`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body,
    });
    const data = await res.json().catch(() => ({}));
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
