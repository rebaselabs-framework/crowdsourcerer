import { defineConfig } from "astro/config";
import tailwind from "@astrojs/tailwind";
import node from "@astrojs/node";

export default defineConfig({
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
