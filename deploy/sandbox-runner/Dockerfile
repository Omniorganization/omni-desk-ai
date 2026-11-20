ARG PYTHON_BASE_IMAGE=python:3.11-slim@sha256:174bec68e0451bffabbb08c7d5d21c6b253f772d81d52b9558af97bb3159b761
FROM ${PYTHON_BASE_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /runner
# The runner drives an isolated rootless Podman (or rootless Docker) API socket
# with the docker CLI; production binds only the dedicated runner user's
# socket, never the OmniDesk app container or a rootful engine.
USER root
COPY requirements.runtime.lock /tmp/requirements.runtime.lock
# runner_server imports omnidesk_agent.config (pydantic); the image used to
# ship the source without its locked dependencies and could not start.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates docker.io \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --require-hashes -r /tmp/requirements.runtime.lock \
    && rm -f /tmp/requirements.runtime.lock \
    && useradd -r -u 10002 sandbox \
    && mkdir -p /workspace /srv/omnidesk-sandbox-workspaces \
    && chown -R sandbox:sandbox /workspace /srv/omnidesk-sandbox-workspaces /runner
COPY omnidesk_agent /runner/omnidesk_agent
# Workspace archives are unpacked under TMPDIR. The sandbox containers are
# created by the host engine, which resolves bind-mount sources on the HOST,
# so TMPDIR must be inside the workspace root that is bind-mounted at the same
# path on the host (deploy/docker/docker-compose.full.yml).
ENV TMPDIR=/srv/omnidesk-sandbox-workspaces/tmp \
    OMNIDESK_SANDBOX_ALLOWED_WORKSPACE_ROOT=/srv/omnidesk-sandbox-workspaces
USER sandbox
EXPOSE 18890
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18890/ready', timeout=3)"
CMD ["python", "-m", "omnidesk_agent.sandbox.runner_server"]
