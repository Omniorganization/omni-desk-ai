ARG PYTHON_BASE_IMAGE
FROM ${PYTHON_BASE_IMAGE} AS builder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /build
COPY requirements.lock pyproject.toml README.md /build/
COPY omnidesk_agent /build/omnidesk_agent
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir --require-hashes -r requirements.lock \
    && python -m build --wheel

FROM ${PYTHON_BASE_IMAGE} AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.lock /tmp/requirements.lock
COPY --from=builder /build/dist/*.whl /tmp/
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && python -m pip install --no-cache-dir --no-deps /tmp/*.whl \
    && rm -rf /tmp/*.whl /tmp/requirements.lock \
    && useradd -r -u 10001 omnidesk \
    && mkdir -p /data \
    && chown -R omnidesk:omnidesk /data /app
USER omnidesk
ENV OMNIDESK_CONFIG=/data/config.yaml
EXPOSE 18789
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18789/health', timeout=3)"
CMD ["omnidesk", "--config", "/data/config.yaml", "serve", "--host", "0.0.0.0"]
