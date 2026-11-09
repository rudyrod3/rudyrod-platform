"""Showcase API: a tiny service that reports what is running and where."""

import os
import platform

from fastapi import FastAPI

app = FastAPI(title="rudyrod-showcase", docs_url=None, redoc_url=None, openapi_url=None)


def build_info() -> dict:
    """Build metadata baked in at image build time, plus runtime facts from the pod."""
    return {
        "version": os.getenv("APP_VERSION", "dev"),
        "commit": os.getenv("GIT_SHA", "unknown"),
        "build_time": os.getenv("BUILD_TIME", "unknown"),
        "arch": platform.machine(),
        "python": platform.python_version(),
        # Injected through the Downward API in k8s; fall back for `docker run`.
        "pod": os.getenv("POD_NAME", platform.node()),
        "namespace": os.getenv("POD_NAMESPACE", "local"),
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/info")
def info() -> dict:
    return build_info()
