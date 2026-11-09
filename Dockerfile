# syntax=docker/dockerfile:1.7
# Showcase service: FastAPI API (default CMD) + one-shot heartbeat report (python -m app.report).
# Base pinned by multi-arch index digest; Dependabot bumps it.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# ---- builder: install pinned dependencies into an isolated prefix ----
FROM ${PYTHON_IMAGE} AS builder
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
COPY app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --no-compile --target /opt/deps -r /tmp/requirements.txt

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

# Apply Debian security fixes that have shipped since the base image was built (this
# layer reruns on every commit, since GIT_SHA above changes). Then drop pip and apt:
# the service never installs anything at runtime, so they're only CVE surface.
RUN apt-get update \
 && apt-get upgrade -y \
 && python -m pip uninstall -y pip \
 && dpkg --purge apt libapt-pkg7.0 \
 && rm -rf /root/.cache /var/lib/apt /var/cache/apt /etc/apt

COPY --from=builder /opt/deps /opt/deps
COPY app /srv/app
WORKDIR /srv

# Precompile bytecode now, since the root filesystem is read-only at runtime.
RUN python -m compileall -q /opt/deps /srv/app

# Numeric UID so Kubernetes runAsNonRoot can verify it without /etc/passwd.
USER 10001:10001
EXPOSE 8080

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-server-header", "--no-access-log"]
