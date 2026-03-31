/**
 * POST /api/auth/refresh — client-side token refresh.
 *
 * Reads the `cs_refresh` cookie from the request, calls the backend
 * /v1/auth/refresh endpoint, and sets new cookies on the response.
 *
 * Client-side JavaScript calls this when it gets a 401 on an API call
 * (e.g., polling for notification badges). Since httpOnly cookies can't
 * be read by JS, this server-side proxy handles the cookie exchange.
 */
import type { APIRoute } from "astro";
import { setAuthCookies, clearAuthCookies } from "@/lib/auth";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const POST: APIRoute = async ({ cookies }) => {
  const refreshToken = cookies.get("cs_refresh")?.value;

  if (!refreshToken) {
    return new Response(JSON.stringify({ detail: "No refresh token" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  }

  try {
    const res = await fetch(`${API_URL}/v1/auth/refresh`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });

    const data = await res.json().catch(() => ({}));

    if (res.ok && data.access_token) {
      setAuthCookies(
        cookies as any,
        data.access_token,
        data.refresh_token,
        data.expires_in,
        data.refresh_expires_in,
      );

      return new Response(JSON.stringify({ refreshed: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    // Refresh failed — clear stale cookies
    clearAuthCookies(cookies as any);

    return new Response(JSON.stringify({ detail: "Refresh failed" }), {
      status: 401,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return new Response(JSON.stringify({ detail: "Network error" }), {
      status: 502,
      headers: { "Content-Type": "application/json" },
    });
  }
};
