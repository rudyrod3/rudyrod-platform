import hashlib
import hmac
import json
import time

import pytest
from botocore.exceptions import ClientError

from app.report import sign
from status_api import handler as h

KEYS = {"staging": b"staging-key", "prod": b"prod-key"}


class FakeTable:
    """Just enough of a boto3 Table for the handler: get/put (with the one condition we use)/update."""

    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get((Key["pk"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def put_item(self, Item, ConditionExpression=None, ExpressionAttributeValues=None):
        current = self.items.get((Item["pk"], Item["sk"]))
        if ConditionExpression and current and not current["heartbeat_ts"] < ExpressionAttributeValues[":ts"]:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues, ReturnValues):
        item = self.items.setdefault((Key["pk"], Key["sk"]), dict(Key))
        item["visits"] = item.get("visits", 0) + ExpressionAttributeValues[":one"]
        return {"Attributes": {"visits": item["visits"]}}


@pytest.fixture(autouse=True)
def fake_aws(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(h, "_table", table)
    monkeypatch.setattr(h, "hmac_key", lambda env: KEYS[env])
    monkeypatch.setattr(h, "ALLOWED_ORIGINS", {"https://rudyrod.me", "https://staging.rudyrod.me"})
    monkeypatch.setattr(h, "PREVIEW_ORIGIN_RE", None)
    h._status_cache.clear()
    return table


def event(method, path, body=None, headers=None, query=None):
    return {
        "rawPath": path,
        "requestContext": {"http": {"method": method}},
        "headers": headers or {},
        "queryStringParameters": query,
        "body": body,
        "isBase64Encoded": False,
    }


def heartbeat_event(env="prod", ts=None, key=None, **overrides):
    ts = int(time.time()) if ts is None else ts
    payload = {"env": env, "timestamp": ts, "pods_ready": 1, "pods_total": 1, "pods": [], "extra": "dropped"}
    payload.update(overrides)
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = sign(key or KEYS[env], ts, body)
    return event(
        "POST",
        "/heartbeat",
        body.decode(),
        {"x-showcase-timestamp": str(ts), "x-showcase-signature": sig},
    )


def call(evt):
    resp = h.handler(evt)
    return resp["statusCode"], json.loads(resp["body"]) if resp["body"] else None, resp["headers"]


def test_signing_matches_report_contract():
    # app.report.sign and the handler must agree byte-for-byte.
    body, ts = b'{"env":"prod"}', 1_800_000_000
    expected = hmac.new(b"k", b"1800000000." + body, hashlib.sha256).hexdigest()
    assert sign(b"k", ts, body) == f"sha256={expected}"


def test_heartbeat_then_status_is_live(fake_aws):
    status, body, _ = call(heartbeat_event())
    assert status == 200 and body["ok"]

    stored = fake_aws.items[("ENV#prod", "STATUS")]
    assert "extra" not in stored and stored["pods_ready"] == 1

    status, body, headers = call(event("GET", "/status", query={"env": "prod"}))
    assert status == 200
    assert body["stale"] is False
    assert body["status"]["pods_total"] == 1
    assert headers["Cache-Control"] == "public, max-age=60"


def test_status_without_heartbeat_is_stale():
    status, body, _ = call(event("GET", "/status", query={"env": "staging"}))
    assert status == 200
    assert body["stale"] is True and body["status"] is None and body["visits"] == 0


def test_old_heartbeat_is_stale(fake_aws):
    fake_aws.items[("ENV#prod", "STATUS")] = {"pk": "ENV#prod", "sk": "STATUS", "heartbeat_ts": 0}
    _, body, _ = call(event("GET", "/status"))
    assert body["stale"] is True


def test_status_rejects_unknown_env():
    status, _, _ = call(event("GET", "/status", query={"env": "dev"}))
    assert status == 400


def test_heartbeat_bad_signature():
    status, _, _ = call(heartbeat_event(key=b"wrong"))
    assert status == 401


def test_staging_key_cannot_write_prod():
    status, _, _ = call(heartbeat_event(env="prod", key=KEYS["staging"]))
    assert status == 401


def test_heartbeat_outside_window():
    status, _, _ = call(heartbeat_event(ts=int(time.time()) - 600))
    assert status == 401


def test_heartbeat_replay_rejected():
    evt = heartbeat_event()
    assert call(evt)[0] == 200
    assert call(evt)[0] == 409


def test_heartbeat_missing_headers():
    status, _, _ = call(event("POST", "/heartbeat", "{}"))
    assert status == 401


def test_visit_increments():
    assert call(event("POST", "/visit"))[1] == {"visits": 1}
    assert call(event("POST", "/visit"))[1] == {"visits": 2}


def test_cors_allows_only_known_origins():
    _, _, headers = call(event("GET", "/status", headers={"origin": "https://rudyrod.me"}))
    assert headers["Access-Control-Allow-Origin"] == "https://rudyrod.me"

    _, _, headers = call(event("GET", "/status", headers={"origin": "https://evil.example"}))
    assert "Access-Control-Allow-Origin" not in headers


def test_preflight_and_unknown_routes():
    assert h.handler(event("OPTIONS", "/visit"))["statusCode"] == 204
    assert call(event("GET", "/nope"))[0] == 404
    assert call(event("GET", "/visit"))[0] == 405
