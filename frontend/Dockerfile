# syntax=docker/dockerfile:1
#
# Next.js frontend, built for a small runtime image.
#
# Three stages, and the third copies almost nothing. `output: "standalone"` in
# next.config.ts traces the modules the app actually imports and emits a
# self-contained server, so the ~400 MB of node_modules never reaches the
# runtime layer.

# --- deps ------------------------------------------------------------------
FROM node:22-alpine AS deps
WORKDIR /app

# Only the manifests, so this layer is cached until a dependency changes —
# not on every source edit.
COPY package.json package-lock.json ./
RUN npm ci

# --- build -----------------------------------------------------------------
FROM node:22-alpine AS build
WORKDIR /app

ENV NEXT_TELEMETRY_DISABLED=1

COPY --from=deps /app/node_modules ./node_modules
COPY . .

# The generated API types are committed (see scripts/codegen.mjs), so the
# build needs no Python and no running backend. `npm run codegen:check` is a
# CI step against the backend, not part of the image build.
RUN npm run build

# --- runtime ---------------------------------------------------------------
FROM node:22-alpine AS runtime
WORKDIR /app

ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV PORT=3000
ENV HOSTNAME=0.0.0.0

# Never root. A container escape from a Node process that renders patient data
# should not begin with root in the filesystem.
RUN addgroup --system --gid 1001 nodejs \
 && adduser --system --uid 1001 nextjs

# `standalone` already contains a minimal node_modules; static and public are
# served from disk and are not traced into it.
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=build --chown=nextjs:nodejs /app/public ./public

USER nextjs
EXPOSE 3000

# No curl or wget in the image on purpose — fewer tools for anything that gets
# in. Node is already here and can do the check itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:3000/login').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
