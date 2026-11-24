"""Heartbeat report: snapshot this namespace's pods and push a signed status to the Lambda.

Runs as a one-shot CronJob (`python -m app.report`). Uses only the standard library so the
job starts fast and needs nothing beyond the pod's ServiceAccount token.

Signing contract (the Lambda's POST /heartbeat must verify the same way):
    body       = compact JSON payload (UTF-8 bytes, sent as-is)
    timestamp  = Unix seconds, sent in the X-Showcase-Timestamp header
    signature  = hex(HMAC-SHA256(HMAC_KEY, f"{timestamp}." + body))
    header     = X-Showcase-Signature: sha256=<signature>
The Lambda rejects timestamps more than 5 minutes from its own clock.

Environment:
    HMAC_KEY         per-environment signing key (from the `heartbeat` Secret)
    STATUS_API_URL   Lambda Function URL; the report POSTs to <url>/heartbeat
    POD_NAMESPACE    namespace to report on (Downward API); falls back to the SA namespace file
    ENVIRONMENT      optional override; otherwise derived from the namespace (showcase-prod -> prod)
    API_SELECTOR     label selector for the API pods (default app.kubernetes.io/name=showcase-api)
    GIT_SHA          commit baked into the image at build time
    ARGOCD_NAMESPACE namespace of the Argo CD Applications (default argocd)
    ARGOCD_APP       this environment's Application (default: the pod's namespace, e.g. showcase-prod)

The payload also carries `argocd` (revision, sync, health, synced_at) when the job can read
its own Application. That read is best effort: without it (no RBAC yet, Argo CD down) the
heartbeat is still sent, just without `argocd`.
"""

import argparse
import hashlib
import hmac
import json
import os
import platform
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
DEFAULT_API_SELECTOR = "app.kubernetes.io/name=showcase-api"
TIMEOUT_SECONDS = 10
# Optional call; kept short so it can't eat the Job's activeDeadlineSeconds.
ARGOCD_TIMEOUT_SECONDS = 5
SHA_RE = re.compile(r"[0-9a-f]{40}")
ARCH_ALIASES = {"aarch64": "arm64", "x86_64": "amd64"}


def log(level: str, msg: str, **fields) -> None:
    print(json.dumps({"level": level, "msg": msg, **fields}), file=sys.stderr, flush=True)


def namespace() -> str:
    ns = os.getenv("POD_NAMESPACE")
    if ns:
        return ns
    try:
        with open(f"{SA_DIR}/namespace") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "local"


def environment(ns: str) -> str:
    return os.getenv("ENVIRONMENT") or ns.removeprefix("showcase-")


def in_cluster() -> bool:
    return bool(os.getenv("KUBERNETES_SERVICE_HOST")) and os.path.exists(f"{SA_DIR}/token")


def kube_get(path: str, timeout: float = TIMEOUT_SECONDS) -> dict:
    """GET a Kubernetes API path with the pod's own ServiceAccount token."""
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    if ":" in host:  # IPv6 service address
        host = f"[{host}]"
    port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
    with open(f"{SA_DIR}/token") as f:
        token = f.read().strip()
    req = urllib.request.Request(f"https://{host}:{port}{path}", headers={"Authorization": f"Bearer {token}"})
    ctx = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:  # noqa: S310 (https, built above)
        return json.load(resp)


def list_pods(ns: str) -> list[dict]:
    """GET /api/v1/namespaces/<ns>/pods (read-only RBAC in k8s/base/rbac.yaml)."""
    return kube_get(f"/api/v1/namespaces/{urllib.parse.quote(ns)}/pods")["items"]


def summarize_app(app: dict) -> dict | None:
    """Reduce an Argo CD Application to what the panel shows. None without a full commit SHA."""
    status = app.get("status") if isinstance(app, dict) else None
    if not isinstance(status, dict):
        return None

    def field(*keys):
        value = status
        for k in keys:
            value = value.get(k) if isinstance(value, dict) else None
        return value if isinstance(value, str) and value else None

    revision = field("sync", "revision")
    if revision is None or not SHA_RE.fullmatch(revision):
        return None
    summary = {
        "revision": revision,
        "sync": field("sync", "status"),
        "health": field("health", "status"),
        "synced_at": field("operationState", "finishedAt"),
    }
    return {k: v for k, v in summary.items() if v is not None}


def argocd_status(ns: str) -> dict | None:
    """Best effort: this environment's Argo CD sync state, or None if it can't be read."""
    app_ns = os.getenv("ARGOCD_NAMESPACE", "argocd")
    app = os.getenv("ARGOCD_APP") or ns
    path = (
        f"/apis/argoproj.io/v1alpha1/namespaces/{urllib.parse.quote(app_ns)}"
        f"/applications/{urllib.parse.quote(app)}"
    )
    try:
        summary = summarize_app(kube_get(path, timeout=ARGOCD_TIMEOUT_SECONDS))
    except urllib.error.HTTPError as e:  # 403 until argocd/heartbeat-rbac.yaml is applied
        log("warn", "could not read Argo CD Application", app=app, status=e.code)
        return None
    except (OSError, ValueError, KeyError) as e:  # URLError, timeouts, bad JSON, no service env
        log("warn", "could not read Argo CD Application", app=app, error=str(e))
        return None
    if summary is None:
        log("warn", "Argo CD Application has no synced revision", app=app)
    return summary


def matches(labels: dict, selector: str) -> bool:
    """Equality-only label selector (k=v,k2=v2), which is all this job needs."""
    pairs = (term.split("=", 1) for term in selector.split(",") if term)
    return all(labels.get(k.strip()) == v.strip() for k, v in pairs)


def digest_of(image_id: str) -> str | None:
    # containerd reports e.g. ghcr.io/user/repo@sha256:abc...
    _, sep, digest = image_id.rpartition("@")
    return digest if sep and digest.startswith("sha256:") else None


def summarize_pod(pod: dict) -> dict:
    status = pod.get("status", {})
    containers = status.get("containerStatuses") or []
    return {
        "name": pod["metadata"]["name"],
        "phase": status.get("phase", "Unknown"),
        "ready": bool(containers) and all(c.get("ready") for c in containers),
        "restarts": sum(c.get("restartCount", 0) for c in containers),
        "node": pod.get("spec", {}).get("nodeName"),
    }


def is_finished_job_pod(pod: dict) -> bool:
    """A completed or failed run of a Job (the heartbeat): history, not workload.

    Failed pods that don't belong to a Job (an evicted API pod, say) still count,
    because those are worth seeing on the panel.
    """
    phase = pod.get("status", {}).get("phase")
    if phase == "Succeeded":
        return True
    owners = pod["metadata"].get("ownerReferences") or []
    return phase == "Failed" and any(o.get("kind") == "Job" for o in owners)


def build_payload(
    pods: list[dict], ns: str, now: int, selector: str = DEFAULT_API_SELECTOR, argocd: dict | None = None
) -> dict:
    live = [p for p in pods if not is_finished_job_pod(p)]
    summaries = [summarize_pod(p) for p in live]

    digests = sorted(
        {
            d
            for p in live
            if matches(p["metadata"].get("labels") or {}, selector)
            for c in p.get("status", {}).get("containerStatuses") or []
            if (d := digest_of(c.get("imageID", "")))
        }
    )

    machine = platform.machine()
    payload = {
        "env": environment(ns),
        "namespace": ns,
        "timestamp": now,
        "commit": os.getenv("GIT_SHA", "unknown"),
        "node_arch": ARCH_ALIASES.get(machine, machine),
        # Normally one; more than one means a rollout was in progress.
        "image_digests": digests,
        "pods_ready": sum(s["ready"] for s in summaries),
        "pods_total": len(summaries),
        "pods": summaries,
    }
    if argocd is not None:
        payload["argocd"] = argocd
    return payload


def sign(key: bytes, timestamp: int, body: bytes) -> str:
    mac = hmac.new(key, f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def post_heartbeat(base_url: str, key: bytes, payload: dict) -> int:
    if urllib.parse.urlsplit(base_url).scheme != "https":
        raise ValueError("STATUS_API_URL must be an https:// URL")
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ts = payload["timestamp"]
    req = urllib.request.Request(  # noqa: S310 (scheme checked above)
        f"{base_url.rstrip('/')}/heartbeat",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Showcase-Timestamp": str(ts),
            "X-Showcase-Signature": sign(key, ts, body),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:  # noqa: S310
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.report", description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the payload instead of sending it")
    args = parser.parse_args(argv)

    ns = namespace()
    selector = os.getenv("API_SELECTOR", DEFAULT_API_SELECTOR)

    argocd = None
    if in_cluster():
        pods = list_pods(ns)
        argocd = argocd_status(ns)
    elif args.dry_run:
        log("warn", "not running in a cluster; reporting no pods")
        pods = []
    else:
        log("error", "not running in a cluster (no ServiceAccount token)")
        return 2

    payload = build_payload(pods, ns, int(time.time()), selector, argocd)

    if args.dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    # Strip so a trailing newline from `kubectl create secret --from-file` can't break signatures.
    key = os.getenv("HMAC_KEY", "").strip()
    url = os.getenv("STATUS_API_URL", "").strip()
    if not key or not url:
        log("error", "HMAC_KEY and STATUS_API_URL must be set")
        return 2

    status = post_heartbeat(url, key.encode(), payload)
    level = "info" if 200 <= status < 300 else "error"
    log(
        level,
        "heartbeat sent",
        status=status,
        env=payload["env"],
        pods_ready=payload["pods_ready"],
        pods_total=payload["pods_total"],
        argocd_revision=(argocd or {}).get("revision", "")[:7] or None,
    )
    return 0 if level == "info" else 1


if __name__ == "__main__":
    sys.exit(main())
