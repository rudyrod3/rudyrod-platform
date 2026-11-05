import hashlib
import hmac
import json

import pytest

from app import report

DIGEST = "sha256:" + "a" * 64


def pod(name, phase="Running", ready=True, labels=None, image_id=f"ghcr.io/x/y@{DIGEST}"):
    return {
        "metadata": {"name": name, "labels": labels or {}},
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
