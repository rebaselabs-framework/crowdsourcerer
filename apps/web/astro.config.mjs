import { defineConfig } from "astro/config";
import tailwind from "@astrojs/tailwind";
import node from "@astrojs/node";

// Public site URL — canonical links, OG tags, sitemap. Override per
// environment with PUBLIC_SITE_URL so staging / preview / white-label
// deployments emit correct metadata.
const SITE_URL =
  process.env.PUBLIC_SITE_URL ?? "https://crowdsourcerer.rebaselabs.online";

export default defineConfig({
  site: SITE_URL,
  output: "server",
  adapter: node({ mode: "standalone" }),
  integrations: [tailwind()],
  server: {
    port: 4321,
  },
  // The Astro server runs internally at localhost:4321 behind Traefik.
  // Astro's default CSRF check compares the request Origin to the server's
  // perceived URL — which would be the internal host, not the public domain.
  // This causes all SSR form submissions (register, login, etc.) to 403.
  // Security is enforced at the FastAPI layer (JWT, bcrypt, rate limiting).
  security: {
    checkOrigin: false,
  },
});
