/**
 * GET /api/workers/[id]/task-stats  →  GET /v1/workers/{id}/task-stats
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const GET: APIRoute = async ({ params }) => {
  try {
    const res = await fetch(`${API_URL}/v1/workers/${params.id}/task-stats`, {
      headers: { "Content-Type": "application/json" },
    });
    const data = await res.json().catch(() => []);
    return new Response(JSON.stringify(data), {
      status: res.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify([]), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
};
