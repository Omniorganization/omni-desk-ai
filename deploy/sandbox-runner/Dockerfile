ARG PYTHON_BASE_IMAGE=python:3.11-slim@sha256:f9fa7f851e38bfb19c9de3afbc4b86ae7176ea7aaf94535c31df5458d5849457
FROM ${PYTHON_BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /runner
# The runner can target an isolated rootless Docker/Podman-compatible socket.
# docker.io provides a CLI; production deployments should bind only the dedicated
# runner-node socket, never the OmniDesk app container.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates docker.io \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -r -u 10002 sandbox \
    && mkdir -p /workspace /srv/omnidesk-sandbox-workspaces \
    && chown -R sandbox:sandbox /workspace /srv/omnidesk-sandbox-workspaces /runner
COPY omnidesk_agent /runner/omnidesk_agent
USER sandbox
EXPOSE 18890
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18890/ready', timeout=3)"
CMD ["python", "-m", "omnidesk_agent.sandbox.runner_server"]
