/**
 * DELETE /api/payouts/[payoutId] — proxy to DELETE /v1/payouts/{id}
 * Cancel a payout request.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const DELETE: APIRoute = async ({ cookies, params }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return new Response(JSON.stringify({ detail: "Not authenticated" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  const res = await fetch(`${API_URL}/v1/payouts/${params.payoutId}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${token}` },
  }).catch((err: any) =>
    new Response(JSON.stringify({ detail: err.message }), { status: 502 })
  );

  if ((res as Response).status === 204) {
    return new Response(null, { status: 204 });
  }
  const data = await (res as Response).json().catch(() => ({}));
  return new Response(JSON.stringify(data), {
    status: (res as Response).status,
    headers: { "Content-Type": "application/json" },
  });
};
