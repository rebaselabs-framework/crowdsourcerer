/**
 * Proxy for /api/webhooks/payload-templates/[...path]
 * Forwards all methods (GET, POST, DELETE) to the backend API.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

const handler: APIRoute = async ({ cookies, request, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  const path = params.path ? `/${params.path}` : "";
  const url = new URL(request.url);
  const backendUrl = `${API_URL}/v1/webhooks/payload-templates${path}${url.search}`;

  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
  };

  let body: BodyInit | undefined;
  if (["POST", "PUT", "PATCH"].includes(request.method)) {
    const contentType = request.headers.get("content-type") ?? "application/json";
    headers["Content-Type"] = contentType;
    body = await request.text();
  }

  try {
    const res = await fetch(backendUrl, {
      method: request.method,
      headers,
      body,
    });

    const responseBody = request.method === "DELETE" && res.status === 204
      ? null
      : await res.text();

    return new Response(responseBody, {
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

export const GET = handler;
export const POST = handler;
export const DELETE = handler;
