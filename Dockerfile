ARG PYTHON_BASE_IMAGE=python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457
FROM ${PYTHON_BASE_IMAGE} AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
COPY requirements.runtime.lock requirements.dev.lock pyproject.toml README.md /build/
COPY omnidesk_agent /build/omnidesk_agent
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir --require-hashes -r requirements.dev.lock \
    && python -m build --wheel

FROM ${PYTHON_BASE_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.runtime.lock /tmp/requirements.runtime.lock
COPY --from=builder /build/dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.runtime.lock \
    && python -m pip install --no-cache-dir --no-deps /tmp/*.whl \
    && rm -rf /tmp/*.whl /tmp/requirements.runtime.lock \
    && useradd -r -u 10001 omnidesk \
    && mkdir -p /data \
    && chown -R omnidesk:omnidesk /data /app
USER omnidesk
ENV OMNIDESK_CONFIG=/data/config.yaml
EXPOSE 18789
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18789/health', timeout=3)"
CMD ["omnidesk", "--config", "/data/config.yaml", "serve", "--host", "0.0.0.0"]
