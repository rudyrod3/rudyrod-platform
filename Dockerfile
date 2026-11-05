# syntax=docker/dockerfile:1.7
# Showcase service: FastAPI API (default CMD) + one-shot heartbeat report (python -m app.report).
# Base pinned by multi-arch index digest; Dependabot bumps it.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1

# ---- builder: install pinned dependencies into an isolated prefix ----
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY app/requirements.txt /tmp/requirements.txt
RUN pip install --no-compile --target /opt/deps -r /tmp/requirements.txt

# ---- runtime: no pip, no build tools, non-root, read-only friendly ----
FROM ${PYTHON_IMAGE}

ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown
ARG APP_VERSION=0.1.0

LABEL org.opencontainers.image.title="rudyrod-showcase" \
      org.opencontainers.image.description="Live Infrastructure showcase service for rudyrod.me" \
      org.opencontainers.image.source="https://github.com/rudyrod3/rudyrod-platform" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV PYTHONPATH=/opt/deps \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_SHA=${GIT_SHA} \
    BUILD_TIME=${BUILD_TIME} \
    APP_VERSION=${APP_VERSION}

# pip is only needed at build time; dropping it shrinks the image and the CVE surface.
RUN python -m pip uninstall -y pip \
 && rm -rf /root/.cache

COPY --from=builder /opt/deps /opt/deps
COPY app /srv/app
WORKDIR /srv

# Precompile bytecode now, since the root filesystem is read-only at runtime.
RUN python -m compileall -q /opt/deps /srv/app

# Numeric UID so Kubernetes runAsNonRoot can verify it without /etc/passwd.
USER 10001:10001
EXPOSE 8080

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", "--no-access-log"]
