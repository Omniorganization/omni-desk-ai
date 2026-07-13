ARG NODE_BASE_IMAGE=scratch
FROM ${NODE_BASE_IMAGE} AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci --no-audit --no-fund

ARG NODE_BASE_IMAGE=scratch
FROM ${NODE_BASE_IMAGE} AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

ARG NODE_BASE_IMAGE=scratch
FROM ${NODE_BASE_IMAGE} AS runtime
ARG NODE_BASE_IMAGE
ARG NODE_BASE_NAME
ARG NODE_BASE_DIGEST
ARG OMNIDESK_BUILD_SHA=unknown
LABEL org.opencontainers.image.source="https://github.com/Omniorganization/omni-desk-ai" \
      org.opencontainers.image.revision=$OMNIDESK_BUILD_SHA \
      org.opencontainers.image.base.name=$NODE_BASE_NAME \
      org.opencontainers.image.base.digest=$NODE_BASE_DIGEST \
      ai.omnidesk.web.base.image=$NODE_BASE_IMAGE
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000
RUN groupadd --system --gid 10001 nodejs \
    && useradd --system --uid 10001 --gid nodejs --create-home nextjs
COPY --from=build --chown=nextjs:nodejs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nodejs /app/.next/static ./.next/static
COPY --from=build --chown=nextjs:nodejs /app/public ./public
USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
