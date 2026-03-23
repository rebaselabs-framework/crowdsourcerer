/**
 * POST /api/admin/sweep
 * Manually triggers the assignment timeout sweep via the admin API.
 * Redirects back to /admin on completion.
 */
import type { APIRoute } from "astro";

const API_URL = import.meta.env.PUBLIC_API_URL ?? "http://api:8100";

export const POST: APIRoute = async ({ cookies, redirect }) => {
  const token = cookies.get("cs_token")?.value;
  if (!token) {
    return redirect("/login");
  }

  try {
    await fetch(`${API_URL}/v1/admin/sweep`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
    });
  } catch {
    // Best-effort; redirect regardless
  }

  return redirect("/admin");
};
