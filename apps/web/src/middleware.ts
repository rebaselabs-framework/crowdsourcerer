/**
 * Astro middleware: proxy /v1/* requests to the internal FastAPI service.
 *
 * Problem: The Astro web server (port 4321) is the only publicly-exposed
 * service. The FastAPI backend (port 8100) is internal-only (Docker network).
 * Links to /v1/* (e.g. Google OAuth, email verification) would 404 without
 * this proxy.
 *
 * Solution: Intercept /v1/* requests at the middleware level and forward them
 * to http://api:8100, passing all headers and body through intact.
 */
import { defineMiddleware } from "astro:middleware";

const API_URL =
  (import.meta.env.PUBLIC_API_URL as string | undefined) ?? "http://api:8100";

export const onRequest = defineMiddleware(async (context, next) => {
  const url = new URL(context.request.url);

  // Only proxy /v1/* paths
  if (!url.pathname.startsWith("/v1/")) {
    return next();
  }

  // Special case: FastAPI's built-in OpenAPI spec lives at /openapi.json (no /v1 prefix).
  // Requests to /v1/openapi.json must be forwarded to http://api:8100/openapi.json.
  const backendPath =
    url.pathname === "/v1/openapi.json" ? "/openapi.json" : url.pathname;
  const apiUrl = `${API_URL}${backendPath}${url.search}`;

  // Forward all headers from the original request, except host
  const forwardHeaders = new Headers();
  for (const [key, value] of context.request.headers.entries()) {
    // Strip hop-by-hop / host headers that cause issues across proxies
    if (
      key === "host" ||
      key === "connection" ||
      key === "transfer-encoding" ||
      key === "te" ||
      key === "trailer" ||
      key === "upgrade"
    ) {
      continue;
    }
    forwardHeaders.set(key, value);
  }

  // Determine if we should forward a body
  const method = context.request.method.toUpperCase();
  const hasBody = !["GET", "HEAD", "OPTIONS"].includes(method);

  let body: BodyInit | undefined;
  if (hasBody) {
    // ReadableStream passthrough — works for JSON, form data, file uploads
    body = context.request.body ?? undefined;
  }

  try {
    const apiResponse = await fetch(apiUrl, {
      method,
      headers: forwardHeaders,
      body,
      // Do NOT follow redirects — pass 3xx responses straight through to the
      // browser so OAuth flows (302 → Google) work correctly
      redirect: "manual",
      // Required for Node.js 18+ when passing a ReadableStream body
      // @ts-ignore — duplex is a Node-specific fetch option
      duplex: "half",
    });

    // Build the response headers to return to the client
    const responseHeaders = new Headers();
    for (const [key, value] of apiResponse.headers.entries()) {
      // Strip hop-by-hop headers
      if (
        key === "connection" ||
        key === "transfer-encoding" ||
        key === "te" ||
        key === "trailer" ||
        key === "upgrade" ||
        key === "keep-alive"
      ) {
        continue;
      }
      responseHeaders.set(key, value);
    }

    return new Response(apiResponse.body, {
      status: apiResponse.status,
      statusText: apiResponse.statusText,
      headers: responseHeaders,
    });
  } catch (err) {
    console.error("[middleware] API proxy error:", err);
    return new Response(
      JSON.stringify({ detail: "API service unavailable" }),
      {
        status: 503,
        headers: { "Content-Type": "application/json" },
      }
    );
  }
});
