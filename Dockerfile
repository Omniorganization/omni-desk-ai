FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md /app/
COPY omnidesk_agent /app/omnidesk_agent
RUN pip install --no-cache-dir -e .
RUN useradd -r -u 10001 omnidesk && mkdir -p /data && chown -R omnidesk:omnidesk /data /app
USER omnidesk
ENV OMNIDESK_CONFIG=/data/config.yaml
EXPOSE 18789
CMD ["omnidesk", "serve", "--config", "/data/config.yaml"]
