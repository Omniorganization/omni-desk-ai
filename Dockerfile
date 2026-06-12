FROM python:3.11-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
COPY pyproject.toml README.md /build/
COPY omnidesk_agent /build/omnidesk_agent
RUN python -m pip install --no-cache-dir --upgrade pip build \
    && python -m build --wheel

FROM python:3.11-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /build/dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir /tmp/*.whl \
    && rm -rf /tmp/*.whl \
    && useradd -r -u 10001 omnidesk \
    && mkdir -p /data \
    && chown -R omnidesk:omnidesk /data /app
USER omnidesk
ENV OMNIDESK_CONFIG=/data/config.yaml
EXPOSE 18789
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18789/health', timeout=3)"
CMD ["omnidesk", "--config", "/data/config.yaml", "serve", "--host", "0.0.0.0"]
