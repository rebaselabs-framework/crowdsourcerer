/**
 * POST /api/disputes/[taskId]/assign-mediator
 * Proxy to POST /v1/disputes/admin/tasks/{taskId}/assign-mediator
 * Admin: assign a mediator to a disputed task.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const POST: APIRoute = async ({ cookies, params, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }
  const body = await request.text();
  try {
    const res = await fetch(
      `${API_URL}/v1/disputes/admin/tasks/${params.taskId}/assign-mediator`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body,
      },
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
