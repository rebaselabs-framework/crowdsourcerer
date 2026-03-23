/**
 * GET  /api/tasks/[id]/dependencies  — list upstream dependencies
 * POST /api/tasks/[id]/dependencies  — add a dependency edge
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

async function proxy(
  method: "GET" | "POST",
  taskId: string,
  token: string,
  body?: string
): Promise<Response> {
  const res = await fetch(`${API_URL}/v1/tasks/${taskId}/dependencies`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    ...(body ? { body } : {}),
  });
  const data = await res.text();
  return new Response(data, {
    status: res.status,
    headers: { "Content-Type": res.headers.get("content-type") ?? "application/json" },
  });
}

export const GET: APIRoute = async ({ cookies, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response('{"detail":"Not authenticated"}', { status: 401 });
  return proxy("GET", params.id!, token);
};

export const POST: APIRoute = async ({ cookies, params, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response('{"detail":"Not authenticated"}', { status: 401 });
  const body = await request.text();
  return proxy("POST", params.id!, token, body);
};
