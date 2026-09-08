"""Shared auth for continuous monitoring worker (Lambda / BFF scheduler)."""

from __future__ import annotations

import hmac
import os
from typing import Any


def _headers_lower(event: dict[str, Any]) -> dict[str, str]:
    raw = event.get("headers") or {}
    return {str(k).lower(): str(v) for k, v in raw.items()}


def monitoring_worker_secret_ok(event: dict[str, Any]) -> bool:
    expected = (os.environ.get("MONITORING_WORKER_SECRET") or "").strip()
    if not expected:
        return False
    headers = _headers_lower(event)
    got = (headers.get("x-monitoring-worker-secret") or "").strip()
    if not got:
        auth = headers.get("authorization") or ""
        if auth.lower().startswith("bearer "):
            token = auth[7:].strip()
            if token and "." not in token:
                got = token
    if not got:
        return False
    return hmac.compare_digest(got, expected)


def monitoring_worker_claims() -> dict[str, Any]:
    return {"role": "monitoring_worker", "worker": True}
