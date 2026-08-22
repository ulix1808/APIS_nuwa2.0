"""Lambda EventBridge: due monitorings → enqueue rescreen en BFF (sin screening)."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float = 20.0) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8") or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"raw": raw}
            return int(resp.status), parsed if isinstance(parsed, dict) else {"data": parsed}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") if e.fp else ""
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {"message": raw}
        return int(e.code), parsed if isinstance(parsed, dict) else {"message": str(e)}
    except Exception as e:
        return 503, {"message": str(e), "code": "UPSTREAM_ERROR"}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    api_base = (os.environ.get("NUWA_API_BASE") or "").rstrip("/")
    bff_base = (os.environ.get("MONITORING_BFF_URL") or "").rstrip("/")
    secret = (os.environ.get("MONITORING_WORKER_SECRET") or "").strip()
    due_limit = int(os.environ.get("MONITORING_DUE_LIMIT") or "50")

    if not api_base or not bff_base or not secret:
        return {
            "ok": False,
            "error": "Missing NUWA_API_BASE, MONITORING_BFF_URL, or MONITORING_WORKER_SECRET",
            "due_count": 0,
            "enqueued": 0,
            "failed": 0,
        }

    secret_headers = {"x-monitoring-worker-secret": secret}
    due_url = f"{api_base}/v1/entities/monitoring/due"
    status, due_body = _post_json(due_url, {"limit": due_limit}, secret_headers)
    if status >= 400:
        return {
            "ok": False,
            "error": "due_failed",
            "due_status": status,
            "due_body": due_body,
            "due_count": 0,
            "enqueued": 0,
            "failed": 0,
        }

    items = due_body.get("items") if isinstance(due_body.get("items"), list) else []
    enqueue_url = f"{bff_base}/api/monitoring/enqueue-rescreen"
    enqueued = 0
    failed = 0
    errors: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            failed += 1
            continue
        payload = {
            "trigger": "scheduler",
            "clientId": item.get("clientId"),
            "entityId": item.get("entityId"),
            "monitoringId": item.get("monitoringId"),
            "entityName": item.get("entityName"),
            "partyType": item.get("partyType"),
            "lastReportFolio": item.get("lastReportFolio"),
            "createdByUserId": item.get("createdByUserId"),
            "sources": item.get("sources"),
            "frequency": item.get("frequency"),
            "rfc": item.get("rfc"),
            "curp": item.get("curp"),
        }
        st, resp = _post_json(enqueue_url, payload, secret_headers, timeout=15.0)
        if 200 <= st < 300:
            enqueued += 1
        else:
            failed += 1
            errors.append(
                {
                    "clientId": item.get("clientId"),
                    "entityId": item.get("entityId"),
                    "status": st,
                    "body": resp,
                }
            )

    result = {
        "ok": failed == 0,
        "due_count": len(items),
        "enqueued": enqueued,
        "failed": failed,
        "errors": errors[:20],
    }
    print(json.dumps({"monitoring_due_tick": result}, ensure_ascii=False))
    return result
