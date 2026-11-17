"""Small shared helpers: canonical JSON, hashing, timestamps, HTTP client."""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def validate_waivers(waivers) -> list[dict]:
    """Reject malformed/unsafe waiver requests before any check runs."""
    from .checker.checks import WAIVABLE_CODES

    if waivers in (None, ""):
        return []
    if not isinstance(waivers, list):
        raise ValueError("waivers must be a list")
    cleaned = []
    for entry in waivers:
        if not isinstance(entry, dict):
            raise ValueError("each waiver must be an object")
        code = str(entry.get("code") or "")
        note = str(entry.get("note") or "").strip()
        if code not in WAIVABLE_CODES:
            raise ValueError(
                f"conflict {code!r} cannot be waived; only "
                f"{sorted(WAIVABLE_CODES)} are operator-acceptable")
        if not note:
            raise ValueError(f"waiver for {code!r} needs a non-empty note")
        cleaned.append({"code": code, "note": note[:500],
                        "at": entry.get("at") or utcnow()})
    return cleaned


def sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def short_hash(*parts: str) -> str:
    return sha256_hex("\x1f".join(parts))[:16]


def http_json(url: str, method: str = "GET", payload=None, timeout: float = 10.0):
    """Call a sibling service; returns (status, parsed_json)."""
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = canonical_json(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = {"error": raw or exc.reason}
        return exc.code, parsed
