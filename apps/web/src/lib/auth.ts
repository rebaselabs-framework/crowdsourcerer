import type { AstroCookies } from "astro";
import type { AstroGlobal } from "astro";
import { apiFetch } from "./api";

/**
 * Cookie durations (seconds).
 * Access token: 30 minutes.  Refresh token: 30 days.
 */
export const ACCESS_TOKEN_MAX_AGE = 60 * 30;          // 30 min
export const REFRESH_TOKEN_MAX_AGE = 60 * 60 * 24 * 30;  // 30 days

/**
 * Set both auth cookies on the Astro response.
 */
export function setAuthCookies(
  cookies: AstroCookies,
  accessToken: string,
  refreshToken: string | null | undefined,
  expiresIn?: number,
  refreshExpiresIn?: number,
) {
  cookies.set("cs_token", accessToken, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    maxAge: expiresIn ?? ACCESS_TOKEN_MAX_AGE,
    path: "/",
  });

  if (refreshToken) {
    cookies.set("cs_refresh", refreshToken, {
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      maxAge: refreshExpiresIn ?? REFRESH_TOKEN_MAX_AGE,
      path: "/",
    });
  }
}

/**
 * Clear both auth cookies (logout).
 */
export function clearAuthCookies(cookies: AstroCookies) {
  cookies.delete("cs_token", { path: "/" });
  cookies.delete("cs_refresh", { path: "/" });
}

/**
 * requireAuth — helper for SSR pages that require a logged-in user.
 *
 * 1. If `cs_token` cookie exists, return it immediately.
 * 2. If missing but `cs_refresh` exists, attempt a transparent refresh:
 *    call POST /v1/auth/refresh, set new cookies, return new access token.
 * 3. If neither exists (or refresh fails), redirect to /login.
 */
export async function requireAuth(
  Astro: AstroGlobal
): Promise<{ token: string; user: null }> {
  // Fast path: access token still valid
  const token = Astro.cookies.get("cs_token")?.value;
  if (token) {
    return { token, user: null };
  }

  // Try transparent refresh
  const refreshToken = Astro.cookies.get("cs_refresh")?.value;
  if (refreshToken) {
    try {
      const data = await apiFetch<{
        access_token: string;
        refresh_token?: string;
        expires_in?: number;
        refresh_expires_in?: number;
      }>("/v1/auth/refresh", {
        method: "POST",
        body: JSON.stringify({ refresh_token: refreshToken }),
      });

      if (data.access_token) {
        setAuthCookies(
          Astro.cookies,
          data.access_token,
          data.refresh_token,
          data.expires_in,
          data.refresh_expires_in,
        );
        return { token: data.access_token, user: null };
      }
    } catch {
      // Refresh failed — fall through to redirect
    }
  }

  // No valid token and refresh failed — clear stale cookies and redirect
  clearAuthCookies(Astro.cookies);
  throw Astro.redirect("/login");
}
