import type { APIRoute } from "astro";
import { apiFetch, getToken } from "@/lib/api";

export const POST: APIRoute = async ({ cookies, params }) => {
  const token = getToken(cookies);
  if (!token) return new Response("Unauthorized", { status: 401 });

  const alertId = params.id;
  try {
    const data = await apiFetch<any>(`/v1/admin/alerts/${alertId}/resolve`, {
      token,
      method: "POST",
    });
    return new Response(JSON.stringify(data), {
      headers: { "Content-Type": "application/json" },
    });
  } catch (e: any) {
    return new Response(JSON.stringify({ error: e.message }), { status: 500 });
  }
};
