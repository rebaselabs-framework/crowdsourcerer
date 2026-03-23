/**
 * POST /api/tasks/[id]/assign-team   → POST /v1/tasks/{id}/assign-team
 * DELETE /api/tasks/[id]/assign-team → DELETE /v1/tasks/{id}/assign-team
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

async function proxy(method: string, params: Record<string, string | undefined>, cookies: any, body?: string) {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  try {
    const res = await fetch(`${API_URL}/v1/tasks/${params.id}/assign-team`, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      ...(body ? { body } : {}),
    });
    if (res.status === 204) return new Response(null, { status: 204 });
    const data = await res.json().catch(() => ({}));
    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ detail: "Network error" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
}

export const POST: APIRoute = async ({ params, cookies, request }) => {
  const body = await request.text();
  return proxy("POST", params as Record<string, string | undefined>, cookies, body);
};

export const DELETE: APIRoute = async ({ params, cookies }) => {
  return proxy("DELETE", params as Record<string, string | undefined>, cookies);
};
