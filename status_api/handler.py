"""Status API: the one thing rudyrod.me depends on. Runs as a Lambda behind a Function URL.

Routes:
    GET  /status?env=prod   STATUS + BUILD + VISITS for the panel, with `stale` when the heartbeat is old
    POST /heartbeat         signed status from the k3s CronJob (see app/report.py for the contract)
    POST /visit             atomically increments the visitor counter

Table layout (`pk` / `sk`):
    ENV#<env>   / STATUS   written here, from verified heartbeats
    BUILD#<env> / BUILD    written by GitHub Actions through OIDC (its own partition, so IAM
                           dynamodb:LeadingKeys can stop CI from touching anything else)
    SITE        / VISITS   written here, from POST /visit
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError

ENVS = ("staging", "prod")
STALE_AFTER_SECONDS = 15 * 60
MAX_CLOCK_SKEW_SECONDS = 5 * 60
MAX_BODY_BYTES = 32 * 1024
# Reserved concurrency caps us at two warm containers, so this bounds reads to a few per 10 s.
STATUS_CACHE_SECONDS = 10
KEY_CACHE_SECONDS = 5 * 60
# The only heartbeat fields we store; anything else in the payload is dropped.
HEARTBEAT_FIELDS = ("namespace", "commit", "node_arch", "image_digests", "pods_ready", "pods_total", "pods")

TABLE_NAME = os.environ.get("TABLE_NAME", "rudyrod-showcase")
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/showcase")
REGION = os.environ.get("AWS_REGION", "unknown")
ALLOWED_ORIGINS = {o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}
_preview = os.environ.get("PREVIEW_ORIGIN_REGEX", "")
PREVIEW_ORIGIN_RE = re.compile(_preview) if _preview else None

_table = None
_ssm = None
_keys: dict[str, tuple[bytes, float]] = {}
_status_cache: dict[str, tuple[dict, float]] = {}


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def table():
    global _table
    if _table is None:
        _table = boto3.resource("dynamodb").Table(TABLE_NAME)
    return _table


def hmac_key(env: str) -> bytes:
    """Per-environment signing key from SSM (SecureString), cached briefly so rotation takes effect."""
    global _ssm
    cached = _keys.get(env)
    if cached and time.time() - cached[1] < KEY_CACHE_SECONDS:
        return cached[0]
    if _ssm is None:
        _ssm = boto3.client("ssm")
    try:
        value = _ssm.get_parameter(Name=f"{SSM_PREFIX}/{env}/hmac", WithDecryption=True)["Parameter"]["Value"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ParameterNotFound":
            raise HttpError(401, "no key for this environment") from e
        raise
    key = value.encode()
    _keys[env] = (key, time.time())
    return key


# ---- helpers ---------------------------------------------------------------


def plain(value):
    """DynamoDB returns numbers as Decimal; turn them back into JSON-friendly ints/floats."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def cors_headers(origin: str | None) -> dict:
    if not origin:
        return {}
    if origin not in ALLOWED_ORIGINS and not (PREVIEW_ORIGIN_RE and PREVIEW_ORIGIN_RE.fullmatch(origin)):
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Max-Age": "86400",
        "Vary": "Origin",
    }


def respond(status: int, body: dict | None, origin: str | None, extra_headers: dict | None = None) -> dict:
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    headers.update(cors_headers(origin))
    headers.update(extra_headers or {})
    return {"statusCode": status, "headers": headers, "body": "" if body is None else json.dumps(body)}


def raw_body(event: dict) -> bytes:
    body = event.get("body") or ""
    data = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    if len(data) > MAX_BODY_BYTES:
        raise HttpError(413, "body too large")
    return data


# ---- routes ----------------------------------------------------------------


def get_status(event: dict) -> dict:
    env = (event.get("queryStringParameters") or {}).get("env", "prod")
    if env not in ENVS:
        raise HttpError(400, f"env must be one of {', '.join(ENVS)}")

    now = time.time()
    cached = _status_cache.get(env)
    if cached and now - cached[1] < STATUS_CACHE_SECONDS:
        return cached[0]

    t = table()
    status = t.get_item(Key={"pk": f"ENV#{env}", "sk": "STATUS"}).get("Item")
    build = t.get_item(Key={"pk": f"BUILD#{env}", "sk": "BUILD"}).get("Item")
    visits = t.get_item(Key={"pk": "SITE", "sk": "VISITS"}).get("Item")

    last = int(status["heartbeat_ts"]) if status else None
    age = int(now) - last if last is not None else None
    body = {
        "env": env,
        "region": REGION,
        "stale": age is None or age > STALE_AFTER_SECONDS,
        "last_heartbeat": last,
        "age_seconds": age,
        "status": plain({k: v for k, v in status.items() if k not in ("pk", "sk")}) if status else None,
        "build": plain({k: v for k, v in build.items() if k not in ("pk", "sk")}) if build else None,
        "visits": plain(visits["visits"]) if visits else 0,
    }
    _status_cache[env] = (body, now)
    return body


def post_heartbeat(event: dict) -> dict:
    headers = event.get("headers") or {}  # Function URLs lowercase header names
    body = raw_body(event)

    try:
        ts = int(headers["x-showcase-timestamp"])
        signature = headers["x-showcase-signature"]
    except (KeyError, ValueError) as e:
        raise HttpError(401, "missing or malformed signature headers") from e
    if abs(time.time() - ts) > MAX_CLOCK_SKEW_SECONDS:
        raise HttpError(401, "timestamp outside the allowed window")

    try:
        payload = json.loads(body, parse_float=Decimal)
    except ValueError as e:
        raise HttpError(400, "body is not JSON") from e
    env = payload.get("env") if isinstance(payload, dict) else None
    if env not in ENVS:
        raise HttpError(400, "unknown env")

    # The key is per environment, so a staging key can never write prod status.
    expected = "sha256=" + hmac.new(hmac_key(env), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HttpError(401, "bad signature")
    if payload.get("timestamp") != ts:
        raise HttpError(400, "payload timestamp does not match header")

    item = {k: payload[k] for k in HEARTBEAT_FIELDS if k in payload}
    item.update(pk=f"ENV#{env}", sk="STATUS", heartbeat_ts=ts, received_at=int(time.time()))
    try:
        # Only move forward in time: a replayed (older or identical) heartbeat is rejected.
        table().put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(pk) OR heartbeat_ts < :ts",
            ExpressionAttributeValues={":ts": ts},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise HttpError(409, "stale or replayed heartbeat") from e
        raise
    _status_cache.pop(env, None)
    return {"ok": True, "env": env, "heartbeat_ts": ts}


def post_visit(event: dict) -> dict:
    result = table().update_item(
        Key={"pk": "SITE", "sk": "VISITS"},
        UpdateExpression="ADD visits :one",
        ExpressionAttributeValues={":one": 1},
        ReturnValues="UPDATED_NEW",
    )
    return {"visits": plain(result["Attributes"]["visits"])}


ROUTES = {
    ("GET", "/status"): (get_status, {"Cache-Control": "public, max-age=60"}),
    ("POST", "/heartbeat"): (post_heartbeat, None),
    ("POST", "/visit"): (post_visit, None),
}


def handler(event: dict, context=None) -> dict:
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET")
    path = event.get("rawPath", "/")
    origin = (event.get("headers") or {}).get("origin")

    if method == "OPTIONS":
        return respond(204, None, origin)

    route = ROUTES.get((method, path))
    if route is None:
        known = any(p == path for _, p in ROUTES)
        return respond(
            405 if known else 404, {"error": "method not allowed" if known else "not found"}, origin
        )

    fn, extra_headers = route
    try:
        return respond(200, fn(event), origin, extra_headers)
    except HttpError as e:
        print(json.dumps({"level": "warn", "path": path, "status": e.status, "error": e.message}))
        return respond(e.status, {"error": e.message}, origin)
