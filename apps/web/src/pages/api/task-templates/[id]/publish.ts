/**
 * POST   /api/task-templates/[id]/publish  — publish template to marketplace
 * DELETE /api/task-templates/[id]/publish  — unpublish template
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

async function proxy(
  method: string,
  id: string,
  token: string,
  body?: string
): Promise<Response> {
  const res = await fetch(`${API_URL}/v1/task-templates/${id}/publish`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body,
  });

  if (res.status === 204) {
    return new Response(null, { status: 204 });
  }

  const data = await res.json();
  return new Response(JSON.stringify(data), {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
}

export const POST: APIRoute = async ({ cookies, params, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  const body = await request.text();
  return proxy("POST", params.id!, token, body);
};

export const DELETE: APIRoute = async ({ cookies, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  return proxy("DELETE", params.id!, token);
};
