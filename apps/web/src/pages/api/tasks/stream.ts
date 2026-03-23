/**
 * GET /api/tasks/stream — SSE proxy to GET /v1/tasks/stream
 * Pipes server-sent events from the API to the browser.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const GET: APIRoute = async ({ cookies }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response('data: {"event":"auth_error"}\n\n', {
      status: 200,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
      },
    });
  }

  const upstream = await fetch(`${API_URL}/v1/tasks/stream`, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "text/event-stream",
    },
  });

  if (!upstream.ok || !upstream.body) {
    return new Response('data: {"event":"error"}\n\n', {
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
