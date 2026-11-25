import hashlib
import hmac
import json
import urllib.error

import pytest

from app import report

DIGEST = "sha256:" + "a" * 64


def pod(name, phase="Running", ready=True, labels=None, image_id=f"ghcr.io/x/y@{DIGEST}", owner=None):
    return {
        "metadata": {
            "name": name,
            "labels": labels or {},
            "ownerReferences": [{"kind": owner, "name": "x"}] if owner else [],
        },
        "spec": {"nodeName": "vm"},
        "status": {
            "phase": phase,
            "containerStatuses": [{"ready": ready, "restartCount": 1, "imageID": image_id}],
        },
    }


API = {"app.kubernetes.io/name": "showcase-api"}


def test_payload_counts_live_pods_and_finds_api_digest(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    pods = [
        pod("showcase-api-1", labels=API),
        pod("heartbeat-old", phase="Succeeded", ready=False, image_id="ghcr.io/x/y@sha256:" + "b" * 64),
        pod("heartbeat-now", ready=False),
    ]

    payload = report.build_payload(pods, "showcase-prod", now=1_800_000_000)

    assert payload["env"] == "prod"
    assert payload["pods_total"] == 2
    assert payload["pods_ready"] == 1
    assert payload["image_digests"] == [DIGEST]
    assert payload["node_arch"]
    assert [p["name"] for p in payload["pods"]] == ["showcase-api-1", "heartbeat-now"]


def test_failed_heartbeat_runs_do_not_count_but_failed_api_pods_do(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    pods = [
        pod("showcase-api-1", labels=API, owner="ReplicaSet"),
        pod("showcase-api-evicted", phase="Failed", ready=False, labels=API, owner="ReplicaSet"),
        pod("heartbeat-failed-1", phase="Failed", ready=False, owner="Job"),
        pod("heartbeat-failed-2", phase="Failed", ready=False, owner="Job"),
        pod("heartbeat-now", owner="Job"),
    ]

    payload = report.build_payload(pods, "showcase-staging", now=1_800_000_000)

    assert [p["name"] for p in payload["pods"]] == ["showcase-api-1", "showcase-api-evicted", "heartbeat-now"]
    assert (payload["pods_ready"], payload["pods_total"]) == (2, 3)


def test_environment_override(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert report.environment("anything") == "staging"


def test_digest_of_ignores_tags():
    assert report.digest_of(f"ghcr.io/x/y@{DIGEST}") == DIGEST
    assert report.digest_of("ghcr.io/x/y:latest") is None


def test_selector_matching():
    assert report.matches({"a": "1", "b": "2"}, "a=1,b=2")
    assert not report.matches({"a": "1"}, "a=1,b=2")


def test_signature_matches_contract():
    key, ts, body = b"secret", 1_800_000_000, b'{"env":"prod"}'
    expected = hmac.new(key, b"1800000000." + body, hashlib.sha256).hexdigest()
    assert report.sign(key, ts, body) == f"sha256={expected}"


def test_dry_run_outside_cluster(monkeypatch, capsys):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setenv("POD_NAMESPACE", "showcase-staging")

    assert report.main(["--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["env"] == "staging"
    assert payload["pods_total"] == 0


def test_send_requires_config(monkeypatch):
    monkeypatch.setattr(report, "in_cluster", lambda: True)
    monkeypatch.setattr(report, "list_pods", lambda ns: [])
    monkeypatch.delenv("HMAC_KEY", raising=False)
    assert report.main([]) == 2


def test_rejects_plain_http_url():
    with pytest.raises(ValueError):
        report.post_heartbeat("http://example.com", b"k", {"timestamp": 1})


# ---- Argo CD revision ------------------------------------------------------

SHA = "2ffcaff" + "0" * 33


def application(**status):
    return {"metadata": {"name": "showcase-staging"}, "status": status}


FULL_APP = application(
    sync={"status": "Synced", "revision": SHA},
    health={"status": "Healthy"},
    operationState={"phase": "Succeeded", "finishedAt": "2026-11-24T21:27:10Z"},
)


def test_summarize_full_application():
    assert report.summarize_app(FULL_APP) == {
        "revision": SHA,
        "sync": "Synced",
        "health": "Healthy",
        "synced_at": "2026-11-24T21:27:10Z",
    }


@pytest.mark.parametrize(
    "app",
    [
        application(sync={"status": "Synced"}),  # no revision
        application(sync={"status": "Synced", "revision": "2ffcaff"}),  # short sha
        application(sync={"status": "Synced", "revision": SHA.upper()}),  # not lowercase hex
        application(sync={"status": "Synced", "revision": "v1.2.3"}),  # a tag, not a sha
        application(sync={"revision": 123}),  # not a string
        application(),
        {"metadata": {}},  # no status at all
        [],  # not an object
    ],
)
def test_summarize_requires_full_sha(app):
    assert report.summarize_app(app) is None


def test_summarize_drops_missing_or_non_string_fields():
    app = application(sync={"revision": SHA, "status": None}, health="Healthy")  # no operationState
    assert report.summarize_app(app) == {"revision": SHA}


def test_argocd_status_reads_own_application(monkeypatch):
    calls = []
    monkeypatch.delenv("ARGOCD_NAMESPACE", raising=False)
    monkeypatch.delenv("ARGOCD_APP", raising=False)
    monkeypatch.setattr(report, "kube_get", lambda path, timeout: calls.append((path, timeout)) or FULL_APP)

    assert report.argocd_status("showcase-staging")["revision"] == SHA
    assert calls == [("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/showcase-staging", 5)]


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError("u", 403, "Forbidden", {}, None),
        urllib.error.HTTPError("u", 404, "Not Found", {}, None),
        TimeoutError("timed out"),
        urllib.error.URLError("connection refused"),
        json.JSONDecodeError("bad", "x", 0),
    ],
)
def test_argocd_status_is_best_effort(monkeypatch, error):
    def fail(path, timeout):
        raise error

    monkeypatch.setattr(report, "kube_get", fail)
    assert report.argocd_status("showcase-staging") is None


def test_payload_includes_argocd_only_when_known(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    summary = {"revision": SHA, "sync": "Synced"}

    with_argo = report.build_payload([], "showcase-prod", now=1, argocd=summary)
    without = report.build_payload([], "showcase-prod", now=1)

    assert with_argo["argocd"] == summary
    assert "argocd" not in without


def test_heartbeat_still_sent_when_argocd_unreadable(monkeypatch):
    sent = {}
    monkeypatch.setattr(report, "in_cluster", lambda: True)
    monkeypatch.setattr(report, "list_pods", lambda ns: [])
    monkeypatch.setattr(report, "argocd_status", lambda ns: None)
    monkeypatch.setattr(report, "post_heartbeat", lambda url, key, payload: sent.update(payload) or 200)
    monkeypatch.setenv("HMAC_KEY", "k")
    monkeypatch.setenv("STATUS_API_URL", "https://example.com")

    assert report.main([]) == 0
    assert sent and "argocd" not in sent
