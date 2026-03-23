import type { APIRoute } from "astro";
import { apiFetch, getToken } from "@/lib/api";

export const POST: APIRoute = async ({ params, request, cookies }) => {
  const token = getToken(Object.fromEntries(cookies.getAll().map((c) => [c.name, c.value])));
  if (!token) return new Response(JSON.stringify({ error: "Unauthorized" }), { status: 401 });

  const category = params.category;
  const body = await request.json();

  try {
    const result = await apiFetch<any>(`/v1/worker/skill-quiz/${category}/submit`, {
      method: "POST",
      body: JSON.stringify(body),
      token,
    });
    return new Response(JSON.stringify(result), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  } catch (e: any) {
    return new Response(JSON.stringify({ error: e.message ?? "Submission failed" }), {
      status: 500,
      headers: { "Content-Type": "application/json" },
    });
  }
};
