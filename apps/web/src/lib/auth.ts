import type { AstroGlobal } from "astro";

/**
 * requireAuth — helper for SSR pages that require a logged-in user.
 *
 * Reads the `cs_token` cookie. If missing, throws a redirect to /login
 * (Astro SSR treats thrown Responses as the page response, so the client
 * is redirected transparently).
 *
 * Returns { token, user } where user is currently null (Layout does not
 * consume the user prop at this time — reserved for future use).
 */
export async function requireAuth(
  Astro: AstroGlobal
): Promise<{ token: string; user: null }> {
  const token = Astro.cookies.get("cs_token")?.value;
  if (!token) {
    throw Astro.redirect("/login");
  }
  return { token, user: null };
}
