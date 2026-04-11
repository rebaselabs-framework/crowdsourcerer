FROM node:22-slim AS builder

WORKDIR /app

# Enable corepack for pnpm
RUN corepack enable

# Copy workspace manifests
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml* ./
COPY packages/types/package.json ./packages/types/
COPY packages/sdk/package.json ./packages/sdk/
COPY apps/web/package.json ./apps/web/

# Install deps
RUN pnpm install --frozen-lockfile

# Copy source
COPY packages/types/ ./packages/types/
COPY packages/sdk/ ./packages/sdk/
COPY apps/web/ ./apps/web/

# Build packages first, then web
RUN pnpm --filter @crowdsourcerer/types build
RUN pnpm --filter @crowdsourcerer/sdk build
RUN pnpm --filter @crowdsourcerer/web build

# ─── Runtime ────────────────────────────────────────────────────────────────

FROM node:22-slim AS runner

WORKDIR /app

COPY --from=builder /app/apps/web/dist ./dist
COPY --from=builder /app/apps/web/node_modules ./node_modules

ENV HOST=0.0.0.0
ENV PORT=4321

EXPOSE 4321

# Liveness probe — node-slim has no curl, so use node's http client.
# Hits the marketing landing page (no DB required) and expects HTTP 200.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD node -e "require('http').get('http://localhost:4321/',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "./dist/server/entry.mjs"]
