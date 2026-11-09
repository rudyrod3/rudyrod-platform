from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_healthz():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_info_reads_build_and_pod_env(monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc1234")
    monkeypatch.setenv("BUILD_TIME", "2026-11-02T00:00:00Z")
    monkeypatch.setenv("POD_NAME", "showcase-api-xyz")
    monkeypatch.setenv("POD_NAMESPACE", "showcase-staging")

    body = client.get("/info").json()

    assert body["commit"] == "abc1234"
    assert body["build_time"] == "2026-11-02T00:00:00Z"
    assert body["pod"] == "showcase-api-xyz"
    assert body["namespace"] == "showcase-staging"
    assert body["arch"]


def test_docs_are_disabled():
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
