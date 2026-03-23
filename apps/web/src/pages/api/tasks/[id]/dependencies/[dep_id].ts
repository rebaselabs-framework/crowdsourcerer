/**
 * DELETE /api/tasks/[id]/dependencies/[dep_id] — remove a dependency edge
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const DELETE: APIRoute = async ({ cookies, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) return new Response('{"detail":"Not authenticated"}', { status: 401 });

  const res = await fetch(
    `${API_URL}/v1/tasks/${params.id}/dependencies/${params.dep_id}`,
    {
      method: "DELETE",
      headers: { Authorization: `Bearer ${token}` },
    }
  );

  if (res.status === 204) return new Response(null, { status: 204 });
  const data = await res.text();
  return new Response(data, {
    status: res.status,
    headers: { "Content-Type": "application/json" },
  });
};
