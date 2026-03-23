/**
 * GET /api/tasks/[id]/output-stream — SSE proxy for LLM output streaming.
 * Pipes GET /v1/tasks/{id}/output-stream from the API to the browser.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const GET: APIRoute = async ({ cookies, params, request }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response('data: {"event":"auth_error","detail":"Not authenticated"}\n\n', {
      status: 200,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
      },
    });
  }

  const taskId = params.id;
  const url = new URL(request.url);
  const speed = url.searchParams.get("speed") ?? "40";

  const upstream = await fetch(
    `${API_URL}/v1/tasks/${taskId}/output-stream?speed=${speed}`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: "text/event-stream",
      },
    }
  );

  if (!upstream.ok || !upstream.body) {
    return new Response('data: {"event":"error","detail":"Failed to connect to stream"}\n\n', {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "Connection": "keep-alive",
      "X-Accel-Buffering": "no",
    },
  });
};
