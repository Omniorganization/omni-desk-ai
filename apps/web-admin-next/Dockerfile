ARG NODE_BASE_IMAGE=node:22-bookworm-slim@sha256:e21fc383b50d5347dc7a9f1cae45b8f4e2f0d39f7ade28e4eef7d2934522b752
FROM ${NODE_BASE_IMAGE} AS deps
WORKDIR /app
COPY package*.json ./
RUN npm ci

FROM ${NODE_BASE_IMAGE} AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM ${NODE_BASE_IMAGE} AS runtime
WORKDIR /app
LABEL org.opencontainers.image.title="OmniDesk Web Admin" \
      org.opencontainers.image.description="Digest-pinned, non-root Web Admin runtime" \
      omnidesk.runtime.read_only_rootfs="required"
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0 \
    TMPDIR=/tmp
COPY --from=build /app/public ./public
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
RUN groupadd --system --gid 10001 omnidesk \
    && useradd --system --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin omnidesk \
    && mkdir -p /tmp/omnidesk-next \
    && chown -R omnidesk:omnidesk /app /tmp/omnidesk-next
USER 10001:10001
VOLUME ["/tmp"]
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD ["node", "-e", "const http=require('http');const port=Number(process.env.PORT||3000);const req=http.get({host:'127.0.0.1',port,path:'/',timeout:3000},res=>process.exit(res.statusCode>=200&&res.statusCode<500?0:1));req.on('error',()=>process.exit(1));req.on('timeout',()=>req.destroy());"]
CMD ["node", "server.js"]
