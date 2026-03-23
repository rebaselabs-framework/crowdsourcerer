/**
 * GET /api/tasks/[id]/status
 * Proxies to GET /v1/tasks/{id} — used by client-side polling for live status updates.
 * Returns a minimal JSON payload: { status, assignments_completed, assignments_required }
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const GET: APIRoute = async ({ cookies, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  const { id } = params;

  try {
    const res = await fetch(`${API_URL}/v1/tasks/${id}`, {
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });

    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      return new Response(JSON.stringify(body), {
        status: res.status,
        headers: { "Content-Type": "application/json" },
      });
    }

    const task = await res.json();
    // Return minimal status payload
    return new Response(
      JSON.stringify({
        status: task.status,
        assignments_completed: task.assignments_completed,
        assignments_required: task.assignments_required,
        completed_at: task.completed_at,
        error: task.error,
      }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }
    );
  } catch {
    return new Response(JSON.stringify({ detail: "Network error" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
};
