import type { APIRoute } from "astro";
import { apiFetch, getToken } from "@/lib/api";

export const GET: APIRoute = async ({ request, cookies, url }) => {
  const token = getToken(cookies);
  if (!token) return new Response("Unauthorized", { status: 401 });

  const params = new URLSearchParams(url.search);
  try {
    const data = await apiFetch<any>(`/v1/admin/alerts?${params}`, { token });
    return new Response(JSON.stringify(data), {
      headers: { "Content-Type": "application/json" },
    });
  } catch (e: any) {
    return new Response(JSON.stringify({ error: e.message }), { status: 500 });
  }
};
