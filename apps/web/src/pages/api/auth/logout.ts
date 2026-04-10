/**
 * POST /api/auth/logout — revoke refresh token and clear cookies.
 */
import type { APIRoute } from "astro";
import { clearAuthCookies } from "@/lib/auth";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const POST: APIRoute = async ({ cookies }) => {
  const refreshToken = cookies.get("cs_refresh")?.value;

  // Revoke on backend (best-effort — clear cookies regardless)
  if (refreshToken) {
    try {
      await fetch(`${API_URL}/v1/auth/logout`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
    } catch {
      // Ignore network errors — we still clear cookies
    }
  }

  clearAuthCookies(cookies);

  return new Response(JSON.stringify({ message: "Logged out" }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
};
