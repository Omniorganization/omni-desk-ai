ARG PYTHON_BASE_IMAGE=python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457
ARG OMNIDESK_VERSION=1.12.6+root-monorepo-production-ga-candidate
ARG OMNIDESK_BUILD_SHA=unknown
ARG OMNIDESK_ARTIFACT_SHA256=unknown
ARG OMNIDESK_IMAGE_DIGEST=unknown
FROM ${PYTHON_BASE_IMAGE} AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
COPY requirements.bootstrap.lock requirements.runtime.lock requirements.dev.lock pyproject.toml README.md /build/
COPY omnidesk_agent /build/omnidesk_agent
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.bootstrap.lock \
    && python -m pip install --no-cache-dir --require-hashes -r requirements.dev.lock \
    && python -m build --wheel --no-isolation

FROM ${PYTHON_BASE_IMAGE} AS runtime
ARG OMNIDESK_VERSION=1.12.6+root-monorepo-production-ga-candidate
ARG OMNIDESK_BUILD_SHA=unknown
ARG OMNIDESK_ARTIFACT_SHA256=unknown
ARG OMNIDESK_IMAGE_DIGEST=unknown
LABEL org.opencontainers.image.version=$OMNIDESK_VERSION \
      org.opencontainers.image.revision=$OMNIDESK_BUILD_SHA \
      org.opencontainers.image.source="https://github.com/yinyufan0813-cmyk/omni-desk-ai" \
      omnidesk.wheel.version=$OMNIDESK_VERSION \
      omnidesk.artifact.sha256=$OMNIDESK_ARTIFACT_SHA256 \
      omnidesk.image.digest=$OMNIDESK_IMAGE_DIGEST
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OMNIDESK_VERSION=$OMNIDESK_VERSION \
    OMNIDESK_BUILD_SHA=$OMNIDESK_BUILD_SHA \
    OMNIDESK_ARTIFACT_SHA256=$OMNIDESK_ARTIFACT_SHA256 \
    OMNIDESK_IMAGE_DIGEST=$OMNIDESK_IMAGE_DIGEST
WORKDIR /app
COPY requirements.bootstrap.lock requirements.runtime.lock requirements.enterprise.lock /tmp/
COPY --from=builder /build/dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.bootstrap.lock \
    && python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.runtime.lock \
    && python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.enterprise.lock \
    && python -m pip install --no-cache-dir --no-deps /tmp/*.whl \
    && rm -rf /tmp/*.whl /tmp/requirements.bootstrap.lock /tmp/requirements.runtime.lock /tmp/requirements.enterprise.lock \
    && useradd -r -u 10001 omnidesk \
    && mkdir -p /data \
    && chown -R omnidesk:omnidesk /data /app
USER omnidesk
ENV OMNIDESK_CONFIG=/data/config.yaml
EXPOSE 18789
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18789/ready', timeout=3)"
CMD ["omnidesk", "--config", "/data/config.yaml", "serve", "--host", "0.0.0.0"]
