/**
 * POST /api/triggers/pipelines/[pipelineId]/create
 * Proxy to create a pipeline trigger.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const POST: APIRoute = async ({ cookies, request, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response(JSON.stringify({ detail: "Unauthorized" }), { status: 401 });

  const body = await request.json().catch(() => ({}));
  const res = await fetch(`${API_URL}/v1/pipelines/${params.pipelineId}/triggers`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  const data = await res.json().catch(() => ({}));
  return new Response(JSON.stringify(data), {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
};
