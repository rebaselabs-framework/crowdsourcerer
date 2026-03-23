/**
 * POST /api/tasks/[id]/applications/[appId]/reject
 * Proxies to POST /v1/tasks/{id}/applications/{appId}/reject
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const POST: APIRoute = async ({ params, cookies }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  try {
    const res = await fetch(
      `${API_URL}/v1/tasks/${params.id}/applications/${params.appId}/reject`,
      {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
      }
    );
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
};
